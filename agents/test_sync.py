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


class CheckTest(unittest.TestCase):
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

    def test_reserved_heading(self) -> None:
        doc = sync.render(GOOD + "\n## MPP\n\nlocal\n", SHARED)
        self.assertIn(
            "'## MPP' is reserved for the shared section", sync.check(doc, SHARED)
        )


if __name__ == "__main__":
    unittest.main()
