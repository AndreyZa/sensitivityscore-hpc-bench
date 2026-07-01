"""submit/ — per-backend submission logic for the experiment harness.

Each backend module exposes the same three-function contract used by
run_experiment.py's main loop (docs §4):

    submit_job(job_id, config, profile, overcommit, cfg) -> handle
    wait_for_completion(handle, cfg) -> None  (blocks until terminal state)
    record_result(handle, job_id, config, profile, overcommit, rep, cfg) -> dict

so run_experiment.py can dispatch on cfg["backends"][config] without branching
on config-specific details itself.
"""
