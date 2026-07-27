# Native InDesign COM compatibility notes

These are production constraints learned from a live test in Adobe InDesign 2026 (21.4.1.4) on Windows. Keep them in the worker; do not reintroduce UI workarounds.

| Situation | Required implementation | Why |
| --- | --- | --- |
| The target INDD is open | Refuse native gate/export until it is closed. | An open UI document can have unsaved state and makes the operation non-transactional. |
| Copy working spread | Duplicate the whole generic two-page working spread using `idBefore` the back-cover spread. | It preserves the cover and prevents pages being appended after the back cover. |
| Change legal date | Use individual one-based `Characters.Item(n).Contents` assignments for the existing ten date characters, then exact full-text comparison. | COM rejects range-level `Contents` assignment from PowerShell in this InDesign version. |
| Export review or final PDF | Set `ExportAsSinglePages = false`, `ExportReaderSpreads = false`, and the requested `RasterResolution` (120, 220, or 300). For gender outputs set the explicit verified page range; otherwise use all pages. | `ExportAsSinglePages = true` writes a directory of page PDFs instead of one multi-page PDF; a hard-coded 120 ppi would make approved 220/300-ppi outputs incorrect. |
| Replace an image | Call `frame.Place(source)` on the existing exact frame; do not delete `AllGraphics` first. | InDesign replaces the graphic in place; deleting first doubles COM traffic and can leave a blank frame after interruption. |
| 50-look image import | Process four looks, save/close, verify links, and record progress before the next batch. | A long 100-frame open-document operation can freeze or disconnect InDesign; completed batches remain safely resumable. |
| Apply credits | In the existing exact credits frame, Find/Change every tab into a paragraph return and set every paragraph to `CREDiTs`. | The approved layout uses one styled paragraph per type, brand, price, and article; tabs or a `CREDITS`/default style do not match it. |
| 50-look credit import | Process four look blocks, save/close, verify styled paragraph content, and record `captions.json` progress before the next batch. | A one-shot 264-row Find/Change pass can consume the COM session for an hour without a recovery point. |
| Correct final composition | Keep both image containers fixed. Enter the intended sources and signed shift in `composition-plan.tsv`, then use `ApplyComposition`; it replaces only placed graphics, moves only the left graphic horizontally, records its bounds before/after, and checks the locked frame geometry after every saved batch. | Frame labels, bounds, page-item counts, and credit-frame geometry remain intact; later pair proof is rendered from the resulting saved INDD rather than asserted in a TSV. |
| Protect frame geometry | Record bounds of every labelled left/right image and credits frame at the structure gate; compare them again at every later native gate. | A content-only crop adjustment remains allowed, but moving or resizing its container is a hard failure. |
| Gate success | Require the post-operation check, saved INDD identity, and state-bound evidence file. | A COM call may return without creating the intended deliverable. |

The driver retains a pre-gate `SaveACopy` checkpoint under `control\checkpoints`. It is a recovery artifact, never a source for a later gate.
