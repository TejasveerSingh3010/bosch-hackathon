"""Agent 2: the Reporter. Documents each failure and pinpoints where it started.

reporter.service.Reporter             build and write a report
reporter.service.report_after_experiment   the hook the orchestrator CLI calls
reporter.watch.watch                  automatic reports for failures nobody triggered
python -m reporter --help             command line
"""
