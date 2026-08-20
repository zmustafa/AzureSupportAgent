"""Recovery Readiness — recover from what, in how long, losing how much.

Every other module in this area answers a neighbouring question: Backup Manager answers
*is backup working*, Backup & DR Coverage answers *is it configured*, Assessments answers
*does it meet best practice*, FMEA answers *what could fail and how bad*. None of them
answers the one an application owner actually asks, which is the one this module owns.

It is a **composer**: it owns no collectors except one small Resource Graph reader for
redundancy and native PaaS backup configuration that nothing else captures. Everything else
is joined from sources another module already fills.

See ``docs/improvement-plans/recovery-readiness/`` for the plan and the decisions log.
"""
