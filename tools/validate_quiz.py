#!/usr/bin/env python3
"""Validate CBT/html/quiz.json against the manual (CBT/html/data2.json).

    python tools/validate_quiz.py            # errors + warnings, exit 1 on any error
    python tools/validate_quiz.py --strict   # warnings also fail the run

Run it after editing quiz.json OR data2.json.  It checks, with the standard
library only:

  * structure   - ids unique and prefixed by their topic id, 2-6 distinct
                  options, `answer` in range, a non-empty explanation;
  * coverage    - every section of the manual has at least one question, and
                  no topic id refers to a section that no longer exists;
  * grounding   - each question's `src` phrase (the wording in the manual the
                  answer was written from) still appears in that section's
                  text.  This is what flags a question that has gone stale
                  because the manual was reworded;
  * answer tell - warns when the correct option is conspicuously longer than
                  every distractor (test-wise readers pick the longest answer).

Question ids are append-only: history saved in a reader's browser refers to
them, so never renumber or reuse an id.  Edit the wording freely.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "CBT" / "html" / "data2.json"
QUIZ = ROOT / "CBT" / "html" / "quiz.json"

MAX_OPTIONS = 6          # the quiz UI labels options A-F
TELL_RATIO = 1.6         # correct option longer than the longest distractor by this factor
TELL_SHARE = 0.45        # ... in more than this share of a topic's multiple-choice questions


def strip_html(fragment):
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"</(td|th)>", " | ", fragment, flags=re.I)
    fragment = re.sub(r"</(p|div|h[1-6]|li|tr|ul|ol|table|figure|figcaption)>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(fragment)


def block_text(block):
    kind = block.get("type")
    data = block.get("data") or {}
    if kind == "text":
        return strip_html(data.get("html", ""))
    if kind == "table":
        lines = [str(data.get("caption", "")), " | ".join(map(str, data.get("headers", [])))]
        lines += [" | ".join(strip_html(str(cell)) for cell in row) for row in data.get("rows", [])]
        return "\n".join(lines)
    return ""


def normalise(text):
    """Dashes, quotes, case and whitespace never decide whether a phrase matches."""
    for dash in "—–‑−":
        text = text.replace(dash, "-")
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("children") or [])


def load_sections():
    manual = json.loads(MANUAL.read_text(encoding="utf-8"))
    sections = {}
    for chapter in manual["module"]["categories"]["do"]["chapters"]:
        for node in walk(chapter.get("toc") or []):
            text = "\n".join(block_text(b) for b in (node.get("blocks") or []))
            sections[node["id"]] = {
                "title": f"Ch {chapter['chapterNumber']} - {node['title']}",
                "text": normalise(text),
            }
    return sections


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args()

    errors, warnings = [], []
    sections = load_sections()

    try:
        quiz = json.loads(QUIZ.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {QUIZ}: {exc}")
        return 1

    if quiz.get("version") != 1:
        errors.append(f'"version" must be 1 (found {quiz.get("version")!r})')

    topics = quiz.get("topics")
    if not isinstance(topics, dict) or not topics:
        print('quiz.json has no "topics" object')
        return 1

    for topic_id in sections:
        if topic_id not in topics or not topics[topic_id].get("questions"):
            errors.append(f"{topic_id} ({sections[topic_id]['title']}): no questions")

    seen_ids, seen_text = {}, {}
    total = 0

    for topic_id, topic in topics.items():
        if topic_id not in sections:
            errors.append(f"{topic_id}: not a section id in data2.json (renamed or removed?)")
            continue

        haystack = sections[topic_id]["text"]
        multiple_choice = tells = 0

        for index, q in enumerate(topic.get("questions") or [], start=1):
            total += 1
            qid = q.get("id", f"{topic_id}#{index}")
            where = f"{qid}"

            if not isinstance(q.get("id"), str) or not q["id"].startswith(topic_id + "-"):
                errors.append(f"{where}: id must be a string starting with '{topic_id}-'")
            elif q["id"] in seen_ids:
                errors.append(f"{where}: duplicate id (also in {seen_ids[q['id']]})")
            else:
                seen_ids[q["id"]] = topic_id

            text = q.get("q")
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{where}: empty question text")
            elif text in seen_text:
                errors.append(f"{where}: same question text as {seen_text[text]}")
            else:
                seen_text[text] = qid

            options = q.get("options")
            if (not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS
                    or not all(isinstance(o, str) and o.strip() for o in options)):
                errors.append(f"{where}: needs 2-{MAX_OPTIONS} non-empty string options")
                continue
            if len({normalise(o) for o in options}) != len(options):
                errors.append(f"{where}: options are not all distinct")

            answer = q.get("answer")
            if not isinstance(answer, int) or isinstance(answer, bool) or not 0 <= answer < len(options):
                errors.append(f"{where}: 'answer' must be an index into options")
                continue

            if not str(q.get("explain", "")).strip():
                warnings.append(f"{where}: no explanation (the review screen shows it)")

            src = q.get("src")
            if not src:
                warnings.append(f"{where}: no 'src' phrase, so staleness against the manual can't be checked")
            elif normalise(src) not in haystack:
                errors.append(f"{where}: src phrase not found in {topic_id} any more: {src!r}")

            if len(options) > 2:
                multiple_choice += 1
                wrong = [len(o) for i, o in enumerate(options) if i != answer]
                if len(options[answer]) > TELL_RATIO * max(wrong):
                    tells += 1
                    warnings.append(f"{where}: correct option is much longer than the distractors")

        if multiple_choice >= 4 and tells / multiple_choice > TELL_SHARE:
            warnings.append(f"{topic_id}: the correct answer is the obvious longest option in {tells} of {multiple_choice} questions")

    for message in errors:
        print("ERROR  ", message)
    for message in warnings:
        print("WARNING", message)
    print(f"{total} questions in {len(topics)} topics; {len(errors)} error(s), {len(warnings)} warning(s)")

    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
