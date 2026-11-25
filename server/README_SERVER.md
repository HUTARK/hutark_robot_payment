# Hutark server – robot AI plan recharges

## 1. Make the key pair (once, on Hutark's server)

```bash
pip install cryptography
python3 ai_recharge.py keygen
```

- `hutark_ai_private.key`: **SECRET.** Keep it on the server only, back it
  up safely, and never put it in git or on a robot. Anyone with it can create
  free recharges. If you lose it, every robot needs a new public key.
- `hutark_ai_pubkey.txt`: copy it to every robot's `~/pi_assistant`.

## 2. Pricing

Edit the top of `ai_recharge.py`:

- `PRICES_USD_PER_1M`: OpenAI prices per model, as [input, cached, output].
- `INR_PER_USD`: today's rate, plus any margin.

Every new code carries these values, so changing them updates robots at
their next recharge.

Plans (what the customer pays vs. how much ₹ of usage they get) are in
`PLANS` in `recharge_api.py`.

## 3. Payment flow (Razorpay)

`recharge_api.py` is the production API (full API reference and app
integration guide: `../README.md`):

1. `POST /ai-plan/order {robot_id, plan_id}` returns the Razorpay `order_id`
   (the app opens checkout).
2. `POST /ai-plan/confirm {razorpay_order_id, razorpay_payment_id, razorpay_signature}`
   returns `{code}`. The payment signature is verified, and the same payment
   always returns the same code.
   As a backup, the Razorpay webhook `POST /ai-plan/webhook` (event
   `payment.captured`) issues the same code if the app never calls /confirm.
3. The app sends the code to the robot: `POST /ai_plan/recharge {"code"}`,
   then `POST /ai-plan/applied {recharge_id}`. `GET /ai-plan/pending?robot_id=X`
   lists codes that were issued but not yet applied.

Config comes only from environment variables: `RAZORPAY_KEY_ID`,
`RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `HUTARK_AI_PRIVATE_KEY`,
`AI_PLAN_DB`.

Tests (Razorpay mocked, no network):

```bash
pip install -r requirements.txt pytest
python -m pytest tests
```

Codes must be applied within 7 days of being issued.

## Manual code (offline payment / testing)

```bash
python3 ai_recharge.py sign --robot GIYA001 --credit 300
python3 ai_recharge.py show HTK1....      # check a code
```

## Recommended

Set a monthly **budget limit in the OpenAI dashboard** as a final safety net.
