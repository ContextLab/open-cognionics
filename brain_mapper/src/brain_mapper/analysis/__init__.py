"""The results of a session: spectra, band power, the eyes-closed/eyes-open alpha ratio,
connectivity (dwPLI) per round and over time, and communities. See docs/brain-mapper-results.md."""

from .pipeline import NotComputable, compute, spans
from .run import Job, analyze, finished_dir, save

__all__ = ["Job", "NotComputable", "analyze", "compute", "finished_dir", "save", "spans"]
