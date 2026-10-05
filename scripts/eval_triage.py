"""Measure announcement-triage accuracy on labeled announcements.

Manual, needs the optional `laya` install (pip install -r requirements-triage.txt):

    python scripts/eval_triage.py [tests/fixtures/triage_labeled.json]

Prints precision / recall / F1 at several score thresholds, plus a coarse
temperature sweep (log-loss) to help fit agent.triage.TEMPERATURE and pick a
default min_score. Fixture format: {"items": [{"title", "body", "important"}]}.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import triage  # noqa: E402
from agent.serialize import strip_html  # noqa: E402

THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
TEMPERATURES = (0.5, 1.0, 1.5, 2.0, 3.0)
DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "triage_labeled.json"


def raw_signals(decider, items):
    out = []
    for item in items:
        body = strip_html(item.get("body") or "")[: triage.MODEL_BODY_CHARS]
        out.append(decider.decide({"title": item["title"], "body": body}, triage.QUESTIONS))
    return out


def score_at(signals, temperature):
    return triage.combine_score({q: triage.calibrate(p, temperature) for q, p in signals.items()})


def prf(scores, labels, threshold):
    tp = sum(1 for s, y in zip(scores, labels) if s >= threshold and y)
    fp = sum(1 for s, y in zip(scores, labels) if s >= threshold and not y)
    fn = sum(1 for s, y in zip(scores, labels) if s < threshold and y)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def log_loss(scores, labels):
    eps = 1e-6
    return -sum(
        math.log(min(max(s, eps), 1 - eps)) if y else math.log(min(max(1 - s, eps), 1 - eps))
        for s, y in zip(scores, labels)
    ) / len(labels)


def evaluate(decider, items):
    signals = raw_signals(decider, items)
    labels = [bool(i["important"]) for i in items]
    lines = [f"{len(items)} items, {sum(labels)} important\n", "threshold  precision  recall  f1   (T=%.1f)" % triage.TEMPERATURE]
    scores = [score_at(s, triage.TEMPERATURE) for s in signals]
    for t in THRESHOLDS:
        p, r, f = prf(scores, labels, t)
        lines.append(f"{t:<10} {p:<10.2f} {r:<7.2f} {f:.2f}")
    lines.append("\ntemperature  log-loss (lower is better calibrated)")
    for temp in TEMPERATURES:
        lines.append(f"{temp:<12} {log_loss([score_at(s, temp) for s in signals], labels):.3f}")
    return "\n".join(lines)


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_FIXTURE
    items = json.loads(path.read_text())["items"]
    decider = triage.default_decider()
    if decider is None:
        sys.exit("laya is not installed: pip install -r requirements-triage.txt")
    print(evaluate(decider, items))


if __name__ == "__main__":
    main(sys.argv)
