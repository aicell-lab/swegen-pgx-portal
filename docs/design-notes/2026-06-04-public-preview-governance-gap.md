# Design note: the public-preview governance gap

**Status:** draft, awaiting Wei's decision.
**Source:** Songtao Cheng's portal-testing report, finding 6 (report id
`7185ff72816c4f1096a3cd298ff52437`, submitted 2026-06-02, still pending
in `/admin`; for developers only — do **not** publish to `/community`).
**Last update:** 2026-06-04.

## Problem

After the agent posted a report to `/api/publish_report` and got back
`status: "pending"`, it also ran `svamp serve swegen-report --public`
as part of its normal "show the user the report" workflow. That created
a publicly accessible URL at
`static-swegen-report-*.svc.hypha.aicell.io` containing the **full
HTML report with all embedded plots** — viewable by anyone with the
link, with **no admin review**.

Songtao's framing is correct and worth taking seriously:

> The "pending review" governance model assumes the portal is the sole
> distribution channel. If an agent can publish the report through
> other means (Hypha static hosting, external URLs, etc.), the admin
> review step provides no meaningful gate.

The portal cannot, by construction, stop an agent that has already
produced an HTML report from posting it anywhere else on the internet.
This is not a bug. It is the answer to a design question we have not
yet asked out loud: **what is `/admin` actually gating?**

## What we already have

- The **Guardian** gates everything the agent retrieves from the
  governed kernel. By the time the agent has assembled report HTML,
  every aggregate value, every figure, every byte in it has already
  passed `ensure_output_secure`.
- The **portal** gates the *community-attribution* surface
  (`/community`, `GET /api/community/reports`,
  `/community/reports/{id}/raw.html`). Approved reports are
  durably hosted on `swegen-pgx-portal.hypha.aicell.io`, listed in
  the discovery feed, attributed by name, and counted in the admin
  dashboard.
- **Nothing on the portal binds those two surfaces together.** A
  Guardian-approved report can lawfully exist on a third-party host
  *and* be pending on `/admin` *and* be live on `/community`, in any
  combination. The portal only controls the last of those.

## Three credible postures

The honest question is which of these we want this portal to be. They
are mutually exclusive in their *guarantees*; the operational
differences are small.

### A. "Portal is the official channel" (status quo, but with honest docs)

What `/admin` gates: **endorsement, attribution, durability**. The
portal community page is the only *officially sanctioned* surface for
SweGen PGx Portal reports. Hosting the same HTML elsewhere is
permitted but not endorsed; admin approval moves a report from
"someone's draft" to "a SweGen PGx Portal community report."

- **Cost to add:** a short policy note in SKILL.md and the
  responsible-use banner saying exactly this. No code change.
- **Risk:** none, but the user gets a clearer mental model of what
  "approved" means.
- **What we lose:** the implicit claim that approval prevents public
  visibility. We never really had that claim — we'd just stop
  pretending.

### B. "Portal is the only allowed public surface"

What `/admin` gates: **all public visibility**. The portal forbids
agents from publicly serving the report through any other channel
before approval; pending and rejected reports must remain private.

- **Cost to add:**
  1. New section in SKILL.md / sensitivity contract: "Do not publicly
     share reports before admin approval. The portal community tab is
     the only approved distribution channel."
  2. The Guardian gets a *report-handling* rule that flags
     `svamp serve --public`, `hypha-cloud apps install`, external
     paste-bin POSTs, etc. as policy violations when they are applied
     to report-shaped HTML.
  3. Optional: the agent watermarks any kernel-produced report HTML
     with a `<!-- pending admin review -->` token, and the Guardian
     refuses to release the unwatermarked version to any non-portal
     egress.
- **Risk:** materially weakens the agent experience. The agent
  genuinely needs to be able to show the user a draft before
  submitting. We'd have to design a private-preview surface (a
  per-session ephemeral preview URL on the portal itself, gated by
  the session token) to replace `svamp serve`. That is a real piece
  of engineering, not a one-day fix.
- **What we get:** the "pending review" status becomes a real gate
  with real consequences — admins decide what becomes public, full
  stop.

### C. "Portal is one channel of many, but approval grants attribution"

What `/admin` gates: **attribution and discoverability inside the
SweGen community**. Agents are explicitly allowed to host previews
wherever they like; the portal makes no claim over them. Approval is
the moment a report enters the SweGen PGx community feed and counts
toward portal stats.

- **Cost to add:** docs only; same as posture A, with the explicit
  framing that out-of-portal preview hosting is part of the
  *expected* flow.
- **Risk:** none, but we accept that the dataset's public-aggregate
  conclusions might circulate ahead of admin review. Since the
  dataset is aggregate-only (no PII) and the Guardian already gated
  every value in any report, the actual privacy risk floor is the
  same as A.

## Recommendation

**Posture A** is the right answer for the press-release launch. It's
honest about what we control, requires nothing more than a docs and
SKILL.md edit, and does not weaken the agent flow that Songtao's
test demonstrated works well. Posture B is a future option if a
real-world incident forces it; the engineering work to make it
viable (private-preview URL, Guardian report-egress rule) is large
enough that it should be a deliberate, separate project rather than
a reflexive tightening.

## What we'd actually ship under posture A

A single small PR, no code change:

1. **SKILL.md** — add a *Distributing reports* paragraph:
   > Reports submitted via `publish_report` are reviewed by portal
   > admins before they appear on the portal's community page. The
   > approval step is about portal endorsement and discoverability,
   > not visibility — once your agent has assembled a report, the
   > HTML itself is yours to share. If you do share a draft outside
   > the portal, please be explicit with the recipient that it has
   > not yet been reviewed by SweGen PGx Portal admins.
2. **`/community` page banner** — one sentence under the hero:
   > Reports listed here have been reviewed and endorsed by SweGen
   > PGx Portal admins. Reports hosted elsewhere on the internet
   > carry no such endorsement.
3. **Admin emails** — make it explicit in the submission email that
   the report may already be visible elsewhere; the admin's decision
   is about portal endorsement, not first-time disclosure.

That's it. Maybe 15 lines of edits across two files. Once Wei
signs off on posture A, it can ship in the same patch that closes
issues #2 and #4 (both touch SKILL.md anyway).

## Open questions

1. **Do we want a private-preview URL on the portal itself**, even
   under posture A? It would mean we never have to recommend that
   the agent use `svamp serve` for the draft step — the agent could
   POST the draft to `/api/draft_preview`, get back a short-lived
   per-session preview URL, and show that to the user. Solves the UX
   need for posture B without enforcing B's rules. Probably worth a
   separate issue regardless of which posture we pick.
2. **Should approved reports be served with `Link: <upstream-source>`
   headers** (or a footer link) pointing back to the agent's draft
   URL? That at least makes the relationship between the draft and
   the approved version traceable.
3. **What does Adam (the data owner) want here?** The portal exists
   because Adam is comfortable serving aggregate AF data publicly.
   The narrower question of whether un-reviewed *agent-generated
   narratives* about that data should circulate before review is
   a *portal* policy question more than a data-owner question, but
   he should be in the loop on the call.
