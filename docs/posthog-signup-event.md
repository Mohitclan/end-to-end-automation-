# Sending the signup event from the website's backend (P2.03)

growthstack turns website signups into converted leads by reading one PostHog event,
`user_signed_up` (setting `POSTHOG_SIGNUP_EVENT`). Send it **from the backend**, right
after the account is really created. A browser event can be faked by anyone, because
the project key (`phc_...`) is public.

growthstack reads these person properties, so set them exactly:

| Property | Required | Used for |
| --- | --- | --- |
| `email` | yes | Matching the signup to a lead |
| `first_name`, `last_name` | recommended | The lead's name (`name` is accepted if you only have one field) |

Use the **ingestion** host for your region: `https://eu.i.posthog.com` or
`https://us.i.posthog.com` (not `eu.posthog.com`, which is the app and query API).

## Python (posthog-python 6 or later)

```python
from posthog import Posthog

posthog = Posthog("phc_...", host="https://eu.i.posthog.com")  # create once at startup

def on_signup(user):
    posthog.capture(
        "user_signed_up",
        distinct_id=str(user.id),
        properties={"$set": {"email": user.email, "first_name": user.first_name, "last_name": user.last_name}},
    )

# at process exit (or in short-lived scripts): posthog.shutdown()
```

## Node.js (posthog-node)

```js
import { PostHog } from 'posthog-node'

const posthog = new PostHog('phc_...', { host: 'https://eu.i.posthog.com' }) // create once

export function onSignup(user) {
  posthog.capture({
    distinctId: String(user.id),
    event: 'user_signed_up',
    properties: { $set: { email: user.email, first_name: user.firstName, last_name: user.lastName } },
  })
}

// on shutdown: await posthog.shutdown()
```

## In the browser, after signup

Call `posthog.identify(String(user.id))` once the user is logged in, so their earlier
anonymous visits join the same person. Use the same id as `distinct_id` above.

## Testing without the website

```bash
.venv/bin/python scripts/send_test_signup.py --email you+signup-test@yourcompany.com
.venv/bin/growthstack run-job posthog_pull
```

The address then shows as a converted lead with source "signed up on our website".
Only use internal addresses: this sends a real event into your PostHog project.
