# Responsible use, enforced in code

PRD section 55 states the critical product principle: TerraShield should never
say "threat detected". It should say "significant anomaly detected", and then
show what changed, where, when, the evidence, the confidence, the historical
comparison, and why the model considers it unusual.

This document records where that principle is enforced, because a principle
that lives only in a document is a principle that erodes under deadline.

## The boundary

TerraShield is an **analyst-assistance and monitoring system**. It is not a
targeting system, not an autonomous decision system, and not an attribution
system. The PRD says so in section 1 and the code is built so that crossing
that line requires deliberately adding a capability rather than merely
forgetting to remove one.

## Where it is enforced

**The schema has nowhere to record intent.** `domain.py` has `ChangeEvent`,
`AnomalyFinding` and `Alert`. Every field is either a measurement, a
provenance reference, or a named human's review. There is no field for cause,
attribution, actor or motive. A future contributor who wanted to store an
assessment of intent would have to add a column, which is a reviewable act.

**The vocabulary is analytical.** `Severity` describes how much a finding would
matter if confirmed. `SiteStatus` is `normal`, `watch`, `anomalous`, `stale` —
none of which is a judgement about a person or a state. `anomaly.headline`
produces "significant deviation from the site's historical baseline, driven
by …", and a test asserts that the words *threat*, *hostile*, *enemy*, *attack*
and *intent* never appear in it.

**The copilot refuses.** `copilot.REFUSALS` is a table of question patterns the
system answers with a reason rather than an approximation:

| Asked | Answered |
|---|---|
| whose facility, who built, which country | attribution of ownership cannot be derived from imagery |
| threat, hostile, enemy, intent, planning | this system measures deviation from a site's own history and does not assess intent |
| target, targeting, strike, engage, weapon | TerraShield is an analyst-assistance and monitoring system |
| predict, forecast, will happen | no forecasting model is in use; trends are described as trends |

Each refusal names what the system *can* answer instead. Tests cover all four.

**Every anomaly bundle carries the caveat.** `evidence.for_anomaly` appends,
to every bundle it builds, that the finding is a statistical deviation from the
site's own record, carries no assessment of cause or intent, and that none
should be inferred from the score. It is written into the evidence rather than
left to the interface, so it survives export into a briefing pack.

**Reports lead with what was seen.** `reports.py` opens every report with
coverage, before findings. A weekly report that begins "3 changes detected"
reads as a quiet week and is indistinguishable from a week in which the site
was under cloud for six days and effectively unmonitored. Leading with coverage
makes that difference impossible to miss.

**Findings that one look cannot resolve are held.** `risk.held_for_confirmation`
marks a finding that a single comparison cannot separate from a moveable
object, and `alerts.prioritise` keeps held findings out of the top of the
queue. They remain visible; they do not send an analyst somewhere on the
strength of a maybe.

What releases the hold is the finding being *seen again*, not merely the
existence of other looks. Those are opposite pieces of evidence, and an earlier
version of this function confused them: a change flagged once across four
comparisons that all covered it is better evidence of something moveable than
a change flagged once with nothing to compare against. The first case now stays
held and says so in those terms.

**Reading is audited, not just writing.** In an intelligence system the
sensitive operation is usually a read. `rbac.READ_ACTIONS` covers imagery
access, evidence export, report reads and every copilot interaction, and the
audit log is hash-chained so that editing it is detectable rather than a matter
of trust.

## The demo estate is civilian on purpose

All four demonstration areas are commercial or public infrastructure that
appears on open maps: a container port, a solar park, a dam, and a remote salt
flat. None is a military installation. Beyond the go-to-market argument in PRD
section 52, this means the repository contains no worked example of surveilling
a defence site, and anyone extending it has to make that choice explicitly.

## What this does not cover

These are engineering controls. They do not substitute for export control
review, customer due diligence, or a human rights impact assessment, all of
which a dual-use geospatial product needs before it is sold into a government
account. They make the system's own outputs honest; they do not decide who
should be allowed to run it.
