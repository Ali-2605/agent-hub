from pathlib import Path

import pytest

from agenthub.tasks import catalog_markdown, load_tasks, parse_schedule

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_examples_load_cleanly():
    tasks, errors = load_tasks({"examples": EXAMPLES})
    assert not errors
    assert "examples/disk-usage" in tasks


def test_build_argv_defaults_and_validation():
    tasks, _ = load_tasks({"examples": EXAMPLES})
    t = tasks["examples/disk-usage"]
    assert t.build_argv({}) == ["python3", "scripts/disk_usage.py", "--path", "~", "--top", "10"]
    assert t.build_argv({"top": "3"})[-1] == "3"
    # values are passed as single argv items: no shell injection
    assert t.build_argv({"path": "x; rm -rf /"})[3] == "x; rm -rf /"
    with pytest.raises(ValueError):
        t.build_argv({"nope": "1"})
    with pytest.raises(ValueError):
        tasks["examples/git-status"].build_argv({})


def test_schedule_parse():
    assert parse_schedule("every 30m") == ("every", 1800)
    assert parse_schedule("daily 07:30") == ("daily", (7, 30))
    with pytest.raises(ValueError):
        parse_schedule("sometimes")


def test_catalog_mentions_everything():
    tasks, _ = load_tasks({"examples": EXAMPLES})
    md = catalog_markdown(tasks)
    assert "examples/clean-tmp" in md and "asks the user" in md
