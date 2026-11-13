# Camera Placement: client contract

For the developers who build the Branch side. Everything here describes what the server does today.
You should not need to read server code.

**Camera Placement** = whether one camera films inside the shop (`inside`), outside it (`outside`),
or cannot be told (`uncertain`). The Branch sends one still from one camera; the server asks the
judging model (TypeSafe Jev, through OpenRouter) and returns its answer as it came. **The Branch
decides what to do with the answer.** The server never stores it.

## At a glance

| | |
|---|---|
| When to call | when a Branch is set up, or when a camera is added, moved or replaced. Not on a timer |
| One call | one still from one camera. For 30 cameras, make 30 calls, one after another |
| Answer | Jev's raw answer: `label` (inside / outside / uncertain) with probabilities and confidence, plus `area` |
| Cost | about $0.0002 per call (paid by the server). A loop that calls without stopping costs real money |
| Time | usually 1–2 s. The server gives up after about 30 s |

## 1. URL and headers

```text
POST https://app.advice.th/ai-camera/api/camera-placement
```

```text
Content-Type: application/json
X-Activation-Code: <the Branch's Activation Code, ADV-XXXX-XXXX-XXXX>
Authorization: Bearer <the Branch's Device Key>
```

The same headers the traffic feeds send (`AI_CAMERA_ACTIVATION_CODE`, `AI_CAMERA_DEVICE_KEY`).
Never log the Device Key or the `Authorization` header. Both headers are required; a missing,
wrong or removed key is 401. The client IP does not matter.

## 2. Request body

```json
{
  "jpeg": "<the camera still, JPEG, base64>",
  "camera": "192.168.20.240_ch01"
}
```

| Field | Required | Rule |
|---|---|---|
| `jpeg` | yes | standard base64 of a **JPEG** file (no `data:` prefix). At most **2 MB** before base64. PNG and other formats are refused |
| `camera` | no | your own label for the camera, at most 64 characters. Only written to the server log so we can trace a call; it does not change the answer |

Send the frame as the NVR gives it. **Do not resize it yourself.** The server scales every
picture to what Jev takes (each side 280–1568 px): a 1920×1080 frame is sent as 1568×882,
a 352×288 frame goes unchanged, a 352×198 frame is grown to 498×280. A picture too long and
thin to fit (one side more than 5.6 times the other) is refused with 400.

## 3. Responses and what to do

| Status | Body | Meaning | What the client does |
|---|---|---|---|
| **200** | Jev's answer (section 4) | judged | read `answers.label` and decide |
| **400** | `{"error": "<what is wrong>"}` | the body or the picture is wrong. Nothing was sent to Jev | log `error` and fix it. **Do not resend the same body:** it fails the same way |
| **401** | `{"error": "invalid_device_key"}` | the Device Key was not accepted | do not retry with the same key. The Branch must re-activate |
| **503** | `{"error": "jev_unavailable"}` or `{"error": "Upstream service unavailable"}` | the server or the judging model could not answer now (outage, rate limit, timeout, our account) | temporary. Retry the same body later, e.g. after 1 min, then 5 min, then 30 min. **Do not re-activate** |
| **500** | any | the server failed | same as 503 |

400 messages you may see: `jpeg is not base64`, `jpeg is over 2 MB`, `jpeg must be a JPEG picture`,
`jpeg is not a readable picture`, `body must be {"jpeg": "<base64>"}`,
`camera must be text of at most 64 characters`, `a WxH picture is too long and thin …`,
`a WxH picture is over 40 megapixels`.

Set your HTTP timeout to **60 s**. A timeout on your side is the same as a 503.

## 4. The 200 body

This is OpenRouter's answer **exactly as it came**. The server does not add, remove or rename anything.
A real answer (UNS037, the camera over the car park):

```json
{
  "model": "typesafe/jev-1.14-20261014",
  "answers": {
    "label": {
      "type": "choice",
      "choice": "outside",
      "probabilities": {"inside": 0, "outside": 0.99, "uncertain": 0.01},
      "confidence": 0.98
    },
    "area": {
      "type": "choice",
      "choice": "road_parking",
      "probabilities": {"sales_floor": 0, "cashier": 0, "service": 0, "stock": 0,
                        "road_parking": 0.91, "storefront_outside": 0.09, "no_signal": 0},
      "confidence": 0.89
    }
  },
  "usage": {"input_tokens": 4839, "output_tokens": 115, "cost": 0.000203238},
  "id": "gen-dec-1793256004-Fky9oTWGi5vkIGgxYbjo",
  "provider": "TypeSafe"
}
```

| Field | What it is |
|---|---|
| `answers.label.choice` | the answer: `inside`, `outside` or `uncertain` (the option with the highest probability) |
| `answers.label.probabilities` | one value per option; they add up to 1 |
| `answers.label.confidence` | 0–1, how much the probability sits on one option. Two options close together → low confidence even when one wins (e.g. inside 0.55 / uncertain 0.37 → confidence 0.33) |
| `answers.area.choice` | which kind of place most of the picture shows: `sales_floor`, `cashier`, `service`, `stock`, `road_parking`, `storefront_outside`, `no_signal`. A cross-check only; it does not decide inside/outside |
| `usage.cost` | what this call cost, in USD |
| `model` | the exact model snapshot that answered. Log it with your result |

Read only the fields you need and ignore any others: when the model is upgraded, new fields may appear.

What we saw when testing (29 Oct 2026, two Branches, 58 cameras): most answers had confidence 0.97–1.00;
the low ones were a 16-way split view from the NVR (0.27–0.35) and a camera looking through the front
glass (0.33–0.58). Choosing the confidence below which a person should look is the Branch's decision.

## 5. Example (Python)

```python
import base64, os, requests

def camera_placement(jpeg_bytes, camera):
    r = requests.post(
        "https://app.advice.th/ai-camera/api/camera-placement",
        headers={
            "X-Activation-Code": os.environ["AI_CAMERA_ACTIVATION_CODE"],
            "Authorization": "Bearer " + os.environ["AI_CAMERA_DEVICE_KEY"],
        },
        json={"jpeg": base64.b64encode(jpeg_bytes).decode(), "camera": camera},
        timeout=60,
    )
    if r.status_code == 200:
        label = r.json()["answers"]["label"]
        return label["choice"], label["confidence"]
    if r.status_code in (400, 401):
        raise ValueError(f"camera-placement {r.status_code}: {r.text}")   # fix, do not resend
    return None   # 5xx: retry later
```
