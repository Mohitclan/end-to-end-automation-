"""The team's web app: dashboard, leads, import, approvals, do-not-contact list
and setup (mailboxes, sequences). Run with `growthstack web`."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from growthstack import dnc
from growthstack.config import get_settings
from growthstack.db import get_db
from growthstack.models import (
    Company,
    DncReason,
    DoNotContact,
    Enrollment,
    EnrollmentStatus,
    Lead,
    LeadStage,
    Mailbox,
    ResearchStatus,
    Sequence,
    SequenceStep,
    User,
)
from growthstack.outreach import sequencer
from growthstack.web import auth, dashboard, public
from growthstack.web.templating import templates

log = logging.getLogger(__name__)
PAGE_SIZE = 50


def _render(request: Request, name: str, user: User | None, **ctx):
    ctx.update(user=user, csrf=auth.csrf_token(request), flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def create_app() -> FastAPI:
    settings = get_settings()
    problems = settings.check_production_safety()
    if problems:
        raise RuntimeError("Refusing to start in production:\n- " + "\n- ".join(problems))

    app = FastAPI(title="growthstack", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="gs_session",
        max_age=12 * 3600,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'",
        )
        return response

    @app.exception_handler(auth.LoginRequired)
    async def _login_redirect(request: Request, exc: auth.LoginRequired):
        return RedirectResponse("/login", status_code=303)

    app.include_router(public.router)
    _routes(app)
    return BodySizeLimit(app)


class BodySizeLimit:
    """Reject request bodies over a limit (413) before they are read into memory or
    spooled to disk: 6 MB for the CSV upload, 1 MB for everything else. Set the same
    limits on the reverse proxy as a first line of defence."""

    def __init__(self, app, default: int = 1024 * 1024, overrides: dict[str, int] | None = None):
        self.app = app
        self.default = default
        self.overrides = overrides or {"/import/csv": 6 * 1024 * 1024}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.overrides.get(scope["path"], self.default)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            return await _too_large(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send) -> None:
    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"Request body too large"})


def _routes(app: FastAPI) -> None:  # noqa: C901 - flat list of routes reads best
    csrf = [Depends(auth.check_csrf)]

    # --- login ----------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return _render(request, "login.html", None)

    @app.post("/login", dependencies=csrf)
    def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        ip = request.client.host if request.client else "?"
        if auth.throttle.blocked(ip, email):
            _flash(request, "Too many attempts. Try again in 15 minutes.", "error")
            return _back("/login")
        user = auth.authenticate(db, email, password)
        if not user:
            auth.throttle.fail(ip, email)
            _flash(request, "Wrong email or password.", "error")
            return _back("/login")
        auth.throttle.reset(ip, email)
        request.session.clear()
        request.session["user_id"] = user.id
        return _back("/")

    @app.post("/logout", dependencies=csrf)
    def logout(request: Request):
        request.session.clear()
        return _back("/login")

    # --- dashboard ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        return _render(request, "dashboard.html", user, d=dashboard.build(db))

    # --- leads ----------------------------------------------------------------
    @app.get("/leads", response_class=HTMLResponse)
    def leads(
        request: Request,
        stage: str = "",
        q: str = "",
        page: int = 1,
        user: User = Depends(auth.require_user),
        db: Session = Depends(get_db),
    ):
        stmt = select(Lead).outerjoin(Company)
        if stage:
            stmt = stmt.where(Lead.stage == stage)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(Lead.email.ilike(like), Lead.first_name.ilike(like), Lead.last_name.ilike(like), Company.name.ilike(like))
            )
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page = max(1, page)
        rows = db.scalars(
            stmt.order_by(Lead.score.desc().nulls_last(), Lead.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        ).all()
        return _render(
            request, "leads.html", user, leads=rows, stage=stage, q=q, page=page, total=total,
            pages=max(1, -(-total // PAGE_SIZE)), stages=[s.value for s in LeadStage],
        )

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(lead_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        lead = db.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        return _render(request, "lead.html", user, lead=lead, blocked=dnc.is_blocked(db, lead.email))

    @app.post("/leads/{lead_id}/research", dependencies=csrf)
    def queue_research(lead_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        lead = db.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        if sequencer.open_enrollment(db, lead):
            _flash(request, "This lead already has a draft or an active sequence.", "error")
        else:
            lead.research_status = ResearchStatus.QUEUED
            lead.research_enqueued_at = None
            db.commit()
            _flash(request, "Research queued. The draft will appear under Approvals.")
        return _back(f"/leads/{lead_id}")

    @app.post("/leads/research-bulk", dependencies=csrf)
    def queue_research_bulk(request: Request, limit: int = Form(25), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        rows = db.scalars(
            select(Lead)
            .where(Lead.stage == LeadStage.NEW, Lead.email.is_not(None), Lead.research_status.in_([ResearchStatus.NONE, ResearchStatus.FAILED]))
            .order_by(Lead.id)
            .limit(max(1, min(limit, 200)))
        ).all()
        for lead in rows:
            lead.research_status = ResearchStatus.QUEUED
            lead.research_enqueued_at = None
        db.commit()
        _flash(request, f"Queued research for {len(rows)} new leads.")
        return _back("/leads")

    @app.post("/leads/{lead_id}/stop", dependencies=csrf)
    def stop_lead(lead_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        lead = db.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        sequencer.stop_for_lead(db, lead, reason=EnrollmentStatus.STOPPED_MANUAL, note=f"stopped by {user.email}")
        db.commit()
        _flash(request, "Outreach stopped for this lead.")
        return _back(f"/leads/{lead_id}")

    @app.post("/leads/{lead_id}/stage", dependencies=csrf)
    def set_stage(lead_id: int, request: Request, stage: str = Form(...), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        lead = db.get(Lead, lead_id)
        if not lead or stage not in (LeadStage.CONVERTED, LeadStage.REPLIED, LeadStage.NEW):
            raise HTTPException(400)
        if stage in (LeadStage.CONVERTED, LeadStage.REPLIED):
            sequencer.stop_for_lead(db, lead, reason=EnrollmentStatus.STOPPED_MANUAL, note=f"marked {stage} by {user.email}")
        lead.stage = stage
        db.commit()
        return _back(f"/leads/{lead_id}")

    @app.post("/leads/{lead_id}/company-domain", dependencies=csrf)
    def set_company_domain(lead_id: int, request: Request, domain: str = Form(""), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        # Companies House gives no website, so a person adds it here; Hunter and research then use it.
        from growthstack.leads.importer import normalize_domain

        lead = db.get(Lead, lead_id)
        if not lead or not lead.company:
            raise HTTPException(404)
        bare = normalize_domain(domain)
        taken = bare and db.scalar(select(Company).where(Company.domain == bare, Company.id != lead.company.id))
        if not bare:
            _flash(request, "That doesn't look like a website domain, e.g. acme.co.uk", "error")
        elif taken:
            _flash(request, f"{bare} already belongs to {taken.name}.", "error")
        else:
            lead.company.domain = bare
            db.commit()
            _flash(request, f"Website set to {bare}.")
        return _back(f"/leads/{lead_id}")

    # --- import ---------------------------------------------------------------
    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        s = get_settings()
        companies = db.scalars(select(Company).where(Company.domain.is_not(None)).order_by(Company.name).limit(500)).all()
        return _render(
            request, "import.html", user, companies=companies,
            ch_enabled=bool(s.companies_house_api_key), hunter_enabled=bool(s.hunter_api_key),
        )

    @app.post("/import/csv", dependencies=csrf)
    async def import_csv(
        request: Request,
        file: UploadFile = File(...),
        source_detail: str = Form(...),
        user: User = Depends(auth.require_user),
        db: Session = Depends(get_db),
    ):
        from growthstack.leads.importer import MAX_CSV_BYTES, import_csv as do_import

        data = await file.read(MAX_CSV_BYTES + 1)
        report = do_import(db, data, source_detail=source_detail, imported_by=user.email)
        db.commit()
        msg = (
            f"Imported {report.created} new, updated {report.updated}, skipped {report.skipped_duplicate} duplicates "
            f"and {report.skipped_dnc} on the do-not-contact list."
        )
        if report.errors:
            msg += f" {len(report.errors)} problems: " + "; ".join(report.errors[:5])
        _flash(request, msg, "error" if report.errors and not report.created else "ok")
        return _back("/import")

    @app.post("/import/companies-house", dependencies=csrf)
    def import_ch(
        request: Request,
        name_includes: str = Form(""),
        sic_codes: str = Form(""),
        location: str = Form(""),
        size: int = Form(20),
        user: User = Depends(auth.require_user),
    ):
        from growthstack.jobs.tasks import import_companies_house

        query = {
            "name_includes": name_includes or None,
            "sic_codes": [c.strip() for c in sic_codes.split(",") if c.strip()] or None,
            "location": location or None,
            "size": max(1, min(size, 100)),
        }
        try:
            r = import_companies_house(query, imported_by=user.email)
            _flash(request, f"Companies House: {r['companies']} companies, {r['people']} new directors (no emails yet).")
        except Exception as exc:  # show integration errors to the user
            _flash(request, f"Companies House import failed: {exc}", "error")
        return _back("/import")

    @app.post("/import/hunter", dependencies=csrf)
    def import_hunter(request: Request, company_id: int = Form(...), user: User = Depends(auth.require_user)):
        from growthstack.jobs.tasks import import_hunter as do_hunter

        try:
            r = do_hunter(company_id, imported_by=user.email)
            _flash(request, f"Hunter: {r['created']} new leads ({r['skipped_dnc']} skipped, do-not-contact).")
        except Exception as exc:
            _flash(request, f"Hunter lookup failed: {exc}", "error")
        return _back("/import")

    # --- approvals ------------------------------------------------------------
    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        pending = db.scalars(
            select(Enrollment).where(Enrollment.status == EnrollmentStatus.PENDING_APPROVAL).order_by(Enrollment.created_at)
        ).all()
        return _render(request, "approvals.html", user, pending=pending)

    @app.post("/approvals/{enrollment_id}", dependencies=csrf)
    async def decide(enrollment_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        from starlette.concurrency import run_in_threadpool

        from growthstack.ai.pipeline import resume_review

        form = await request.form()
        action = form.get("action")
        enrollment = db.get(Enrollment, enrollment_id)
        if not enrollment or action not in ("approve", "reject"):
            raise HTTPException(400)
        edits = {}
        for msg in enrollment.messages:
            subject = form.get(f"subject_{msg.step_position}")
            body = form.get(f"body_{msg.step_position}")
            if subject is not None and body is not None and (subject != msg.subject or body != msg.body_text):
                edits[str(msg.step_position)] = [str(subject), str(body)]
        decision = {"action": action, "by": user.email, "edits": edits, "reason": str(form.get("reason") or "")}
        db.close()
        try:
            outcome = await run_in_threadpool(resume_review, enrollment_id, decision)
            _flash(request, "Approved: step 1 is queued to send." if outcome == "approved" else "Draft rejected.")
        except (sequencer.SequencerError, ValueError) as exc:
            _flash(request, str(exc), "error")
        return _back("/approvals")

    # --- do-not-contact -------------------------------------------------------
    @app.get("/dnc", response_class=HTMLResponse)
    def dnc_page(request: Request, q: str = "", user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        stmt = select(DoNotContact).order_by(DoNotContact.created_at.desc())
        if q:
            stmt = stmt.where(or_(DoNotContact.email.ilike(f"%{q}%"), DoNotContact.domain.ilike(f"%{q}%")))
        return _render(request, "dnc.html", user, entries=db.scalars(stmt.limit(500)).all(), q=q)

    @app.post("/dnc", dependencies=csrf)
    def dnc_add(request: Request, value: str = Form(...), note: str = Form(""), user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        value = value.strip().lower()
        if "@" in value and not value.startswith("@"):
            dnc.add(db, email=value, reason=DncReason.MANUAL, note=note or f"added by {user.email}")
        elif "." in value:
            dnc.add(db, domain=value, reason=DncReason.MANUAL, note=note or f"added by {user.email}")
        else:
            _flash(request, "Enter an email address or a domain.", "error")
            return _back("/dnc")
        db.commit()
        _flash(request, f"{value} added. Any outreach to it has been stopped.")
        return _back("/dnc")

    @app.post("/dnc/{entry_id}/delete", dependencies=csrf)
    def dnc_delete(entry_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        entry = db.get(DoNotContact, entry_id)
        if entry and entry.reason in (DncReason.UNSUBSCRIBE, DncReason.COMPLAINT, DncReason.REPLY_OPT_OUT, DncReason.LISTMONK):
            _flash(request, "People who opted out themselves can't be removed from the list.", "error")
            return _back("/dnc")
        dnc.remove(db, entry_id)
        db.commit()
        _flash(request, "Entry removed.")
        return _back("/dnc")

    # --- setup ----------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        s = get_settings()
        integrations = {
            "Resend (sending)": bool(s.resend_api_key) and not s.email_dry_run,
            "Resend webhook": bool(s.resend_webhook_secret),
            f"LLM ({s.llm_provider})": s.llm_provider != "fake",
            "Crawl4AI": bool(s.crawl4ai_url),
            "Firecrawl (backup)": bool(s.firecrawl_api_key),
            "Twenty CRM": bool(s.twenty_api_url and s.twenty_api_key),
            "PostHog query API": bool(s.posthog_personal_api_key and s.posthog_project_id),
            "Search Console": bool(s.gsc_site_url and s.gsc_service_account_file),
            "PageSpeed Insights": bool(s.psi_urls),
            "Sending domain check": bool(s.sending_domain),
            "Companies House": bool(s.companies_house_api_key),
            "Hunter": bool(s.hunter_api_key),
            "listmonk": bool(s.listmonk_url and s.listmonk_api_token),
        }
        return _render(
            request, "setup.html", user,
            mailboxes=db.scalars(select(Mailbox).order_by(Mailbox.id)).all(),
            sequences=db.scalars(select(Sequence).order_by(Sequence.id)).all(),
            integrations=integrations, dry_run=s.email_dry_run,
        )

    @app.post("/setup/mailboxes", dependencies=csrf)
    def add_mailbox(
        request: Request,
        from_email: str = Form(...),
        from_name: str = Form(...),
        reply_to: str = Form(""),
        daily_limit: int = Form(30),
        min_interval_seconds: int = Form(120),
        window_start_hour: int = Form(8),
        window_end_hour: int = Form(17),
        imap_host: str = Form(""),
        imap_user: str = Form(""),
        imap_password_env: str = Form(""),
        user: User = Depends(auth.require_user),
        db: Session = Depends(get_db),
    ):
        if not (0 <= window_start_hour < window_end_hour <= 24) or not (1 <= daily_limit <= 500):
            _flash(request, "Check the sending window (0-24, start before end) and daily limit (1-500).", "error")
            return _back("/setup")
        db.add(
            Mailbox(
                from_email=from_email.strip().lower(), from_name=from_name.strip(), reply_to=reply_to.strip() or None,
                daily_limit=daily_limit, min_interval_seconds=max(30, min_interval_seconds),
                window_start_hour=window_start_hour, window_end_hour=window_end_hour,
                imap_host=imap_host.strip() or None, imap_user=imap_user.strip() or None,
                imap_password_env=imap_password_env.strip() or None,
            )
        )
        db.commit()
        _flash(request, "Mailbox added.")
        return _back("/setup")

    @app.post("/setup/mailboxes/{mailbox_id}/toggle", dependencies=csrf)
    def toggle_mailbox(mailbox_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox:
            mailbox.active = not mailbox.active
            db.commit()
        return _back("/setup")

    @app.post("/setup/sequences", dependencies=csrf)
    async def add_sequence(request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if not name:
            _flash(request, "Give the sequence a name.", "error")
            return _back("/setup")
        seq = Sequence(name=name)
        seq.steps.append(SequenceStep(position=1, delay_hours=0, subject_template="", body_template="(written by the AI and approved per lead)"))
        for pos in range(2, 6):
            body = str(form.get(f"body_{pos}") or "").strip()
            if not body:
                break
            try:
                delay_days = max(1, int(form.get(f"delay_{pos}") or 3))
            except ValueError:
                delay_days = 3
            subject = str(form.get(f"subject_{pos}") or "").strip()
            try:
                sequencer._template_env.from_string(subject + body)  # validate syntax early
            except Exception as exc:
                _flash(request, f"Step {pos} template error: {exc}", "error")
                return _back("/setup")
            seq.steps.append(SequenceStep(position=pos, delay_hours=delay_days * 24, subject_template=subject, body_template=body))
        db.add(seq)
        db.commit()
        _flash(request, f"Sequence '{name}' saved with {len(seq.steps)} steps.")
        return _back("/setup")

    @app.post("/setup/sequences/{sequence_id}/toggle", dependencies=csrf)
    def toggle_sequence(sequence_id: int, request: Request, user: User = Depends(auth.require_user), db: Session = Depends(get_db)):
        seq = db.get(Sequence, sequence_id)
        if seq:
            seq.active = not seq.active
            db.commit()
        return _back("/setup")


app = create_app()
