import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sync

SHARED = "## MPP\n\nShared text.\n"

GOOD = """# repo

What the repository is.

## Commands

```bash
# not a heading
make test
```

## Layout

- src/

## Conventions

- Keep it simple.

## Testing

More.
"""


class RenderTest(unittest.TestCase):
    def test_appends_then_replaces(self) -> None:
        first = sync.render(GOOD, SHARED)
        self.assertTrue(first.endswith(sync.END + "\n"))
        self.assertEqual(sync.check(first, SHARED), [])
        updated = sync.render(first, "## MPP\n\nNew text.\n")
        self.assertIn("New text.", updated)
        self.assertNotIn("Shared text.", updated)
        self.assertEqual(updated.count(sync.END), 1)
        self.assertEqual(sync.render(updated, "## MPP\n\nNew text.\n"), updated)

    def test_cli_render_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp, "shared.md")
            shared.write_text(SHARED)
            target = Path(tmp, "AGENTS.md")
            target.write_text(GOOD)
            self.assertEqual(
                sync.main(["--shared", str(shared), "render", str(target)]), 0
            )
            self.assertEqual(
                sync.main(["--shared", str(shared), "check", str(target)]), 0
            )


WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/agents-sync.yml"


def workflow_render(doc: str, shared: str) -> subprocess.CompletedProcess[str]:
    """Run the render script embedded in agents-sync.yml on doc."""
    text = WORKFLOW.read_text()
    body = text.split("<<'RENDER'\n", 1)[1].split("\n          RENDER\n", 1)[0]
    script = "\n".join(line.removeprefix("          ") for line in body.splitlines())
    with tempfile.TemporaryDirectory() as tmp:
        target, source = Path(tmp, "AGENTS.md"), Path(tmp, "shared.md")
        target.write_text(doc)
        source.write_text(shared)
        result = subprocess.run(
            [sys.executable, "-c", script, str(target), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        result.stdout = target.read_text()
        return result


class WorkflowRenderTest(unittest.TestCase):
    def test_matches_sync_render(self) -> None:
        stale = sync.render(GOOD, "## MPP\n\nOld.\n")
        for doc in (GOOD, stale, sync.render(GOOD, SHARED), "", stale + "\n\n"):
            result = workflow_render(doc, SHARED)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, sync.render(doc, SHARED))

    def test_refuses_missing_end_marker(self) -> None:
        broken = sync.render(GOOD, SHARED).replace(sync.END, "")
        self.assertNotEqual(workflow_render(broken, SHARED).returncode, 0)
        with self.assertRaises(ValueError):
            sync.render(broken, SHARED)


class CheckTest(unittest.TestCase):
    def test_preamble_before_title(self) -> None:
        doc = sync.render("Preamble.\n" + GOOD, SHARED)
        self.assertIn(
            "must start with a '# <repository>' title", sync.check(doc, SHARED)
        )

    def test_missing_section(self) -> None:
        self.assertIn(
            "missing the shared MPP section (run sync.py render)",
            sync.check(GOOD, SHARED),
        )

    def test_stale_section(self) -> None:
        doc = sync.render(GOOD, "## MPP\n\nOld.\n")
        self.assertIn(
            "the shared MPP section is out of date (run sync.py render)",
            sync.check(doc, SHARED),
        )

    def test_section_must_be_last(self) -> None:
        doc = sync.render(GOOD, SHARED) + "\n## Later\n"
        self.assertIn(
            "the shared MPP section must be the last thing in the file",
            sync.check(doc, SHARED),
        )

    def test_required_order(self) -> None:
        doc = sync.render(
            GOOD.replace("## Layout", "## Tmp")
            .replace("## Commands", "## Layout")
            .replace("## Tmp", "## Commands"),
            SHARED,
        )
        self.assertTrue(
            any(
                e.startswith("the first sections must be")
                for e in sync.check(doc, SHARED)
            )
        )

    def test_title_and_intro(self) -> None:
        doc = sync.render(GOOD.replace("What the repository is.\n", ""), SHARED)
        self.assertIn(
            "needs a short paragraph under the title saying what the repository is",
            sync.check(doc, SHARED),
        )
        doc = sync.render("## Commands\n" + GOOD.split("## Commands\n", 1)[1], SHARED)
        self.assertIn(
            "must start with a '# <repository>' title", sync.check(doc, SHARED)
        )

    def test_nested_fences(self) -> None:
        doc = GOOD.replace("- src/", "- src/\n\n````md\n```ts\nx\n```\n## Usage\n````")
        self.assertEqual(sync.check(sync.render(doc, SHARED), SHARED), [])
        self.assertNotIn((2, "Usage"), sync._headings(doc))

    def test_reserved_heading(self) -> None:
        doc = sync.render(GOOD + "\n## MPP\n\nlocal\n", SHARED)
        self.assertIn(
            "'## MPP' is reserved for the shared section", sync.check(doc, SHARED)
        )


if __name__ == "__main__":
    unittest.main()
