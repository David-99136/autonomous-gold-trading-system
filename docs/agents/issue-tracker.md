# Issue tracker: Local Markdown

Issues and specs for this repository live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The specification is `.scratch/<feature-slug>/spec.md`
- Implementation issues use one file per ticket:
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- Ticket numbering begins at `01`
- Triage state is recorded as a `Status:` line near the top
- Comments and conversation history are appended under `## Comments`

## Publishing to the issue tracker

Create a file under `.scratch/<feature-slug>/`, creating the directory when needed.

## Fetching a ticket

Read the referenced Markdown file. The user will normally provide its path or issue number.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md`
- Ticket type: `Type: research`, `prototype`, `grilling`, or `task`
- Ticket state: `Status: claimed` or `resolved`
- Dependencies: `Blocked by: NN, NN`
- Claim work by setting `Status: claimed` before beginning
- Resolve work by adding an `## Answer`, changing the status to `resolved`,
  and recording a context pointer in `map.md`
