---
name: lookbook-layout
description: Create, update, or extend fashion lookbooks in Adobe InDesign from a PDF reference, high-resolution source images, and an Excel credit catalogue. Use for matching paired model photos, treating PDF order as authoritative, allowing unused extra Excel looks, replacing placed images, preserving covers and page order, and delivering a verified lookbook PDF.
---

# Lookbook layout — controlled native automation

Use this skill whenever a user asks to make, update, continue, or export a fashion lookbook. A request such as “Сделай лукбук к 26 июля” starts the complete production workflow, not a partial layout task.

Read `references/gated-control.md` before touching the master. The controller at `scripts/lookbook_gate.py` is the only source of progress. Its evidence must pass in this exact order:

`map → structure → dates → frames → images → captions → visual → release → pdf`

Do not begin, export, or call a later stage complete while `status` reports an earlier `NEXT` gate. A saved INDD, a preview, an alert, or a PDF file is not a passed gate.

## Startup

### Project hygiene — mandatory before source analysis

The project root is the delivery folder. It may contain saved master `.indd` files, review/final `.pdf` files, `Gender`, `control`, `control-history`, and the visible `_MAT` mirror. Every controlled source copy, registry, map, proof card, contact sheet, temporary render, helper script, and scratch file belongs under `control\work`; `_MAT` is refreshed from the frozen snapshot for browsing and handoff, never read as a live source.

After copying the prepared master directly into the dated project folder, run `scripts\create_lookbook_work_area.py <project>`. It creates the required `control\work` structure. Put hires in `control\work\_mat\hires`; use `control\work\scratch` for one-off utilities. The application also maintains `_MAT\hires` and the visible frozen source files at the root. The controller rejects initialization unless all controlled inputs are in `control\work`, and rejects map acceptance, review export, completion, or final publishing while any other temporary root item exists.

For a legacy project that has already passed every gate, use `scripts\migrate_legacy_lookbook_work.py <project> --apply` only after InDesign is closed. It moves the legacy work area, preserves a state backup and migration manifest in `control\history`, rebinds the frozen paths, and verifies that the passed gate state remains intact.

### Reference authority — apply before counting anything

The PDF reference is the source of truth for the deliverable: it determines the required number of looks, their exact order, and each left/right photo pair. Build `look-register.tsv` from the PDF reference and the hires photos first. Covers and non-look source images are not looks. The registry builder detects an optional leading cover from visible page content; it does not assume that PDF page 1 is a cover. A page with exactly two visible photo placements is treated as the first look, including when it is PDF page 1.

The workbook is a credit catalogue only. It may contain extra cards, rejected looks, or looks absent from the PDF. Its image count must never be compared to the PDF/registry count as a precondition. For example, 50 PDF looks and 54 Excel cards is normal: map the 50 required PDF looks, leave 4 Excel cards unused, and continue.

Work strictly outward from the PDF: `PDF page pair → hires pair → Excel card → Excel credit rows`. Never start with Excel order and never ask the user for a sequence merely because the two source counts differ.

### Excel-completeness invariant and two-pass matching

For this workflow, every required PDF look has a corresponding Excel card. Excel may additionally contain unused cards. Treat this as a source guarantee, not an uncertainty to report to the user.

1. First pass: map every visually obvious PDF-pair ↔ Excel-card match and retain it as `PENDING`. Use distinctive garment, bag, shoe, accessory, colour, pose, and model cues. Remove each selected Excel card from the remaining candidate pool.
2. Second pass: consider only the remaining PDF pairs and remaining Excel cards. Resolve them by detailed visual comparison and elimination, then fill the remaining map rows. Do not restart from order or substitute an arbitrary nearest card.
3. Only after all required rows have one-to-one candidates, render the three-panel proof cards. Inspect and confirm them in batches of **at most five cards**; after each viewed batch, change only those exact rows to `CONFIRMED`. A contact sheet is navigation evidence, never sufficient confirmation.

Never call a required PDF look “not found”, pause for a user-supplied table, or abandon the map after the first pass. An Excel card that seems difficult to identify belongs in the second-pass pool, not in an error report.

### Mandatory visual-identification rule

The missing map is work to perform, not input to request. `prepare_caption_mapping.py` renders one card for every required PDF-ordered hires pair; inspect those cards alongside the extracted Excel cards, identify the visual match, and write the map. Do not stop or ask the user for an order/table because a map has not been supplied.

Do not reduce this to file names, row numbers, or counts. The model must use visual cues visible in the images: the model, garment silhouette and colour, accessories, shoes, bag, pose, crop, and background. A formal count check can only verify the completed map; it cannot replace the identification work.

### Runtime and registry integrity

Use the bundled `$py` runtime and only the supplied lookbook-layout scripts for PDF inspection, image preparation, registry validation, and Excel cards. Do not fall back to bare `python`, install a package, or make an improvised helper based on `fitz`/PyMuPDF, `cv2`, SIFT, recursive image globs, or a contact-sheet preview.

Before `init`, build and validate the official `look-register.tsv` with `scripts\build_reference_registry.py <project>`. It reads the controlled reference PDF and immediate JPG hires, matches the embedded left/right reference photographs by image content, writes the complete registry, and creates `control\work\registry-build\<timestamp>` proof cards. On resume it re-verifies an already complete registry against the current reference and refuses only a mismatch; it must never replace a completed registry silently. The PDF page is always the order and left/right authority; extra hires are expected and remain unused. Never start a live job with `create_look_registry.py --count`: it deliberately creates an incomplete schema-only stub for manual fixtures and is invalid production input. A duplicate pair, blank row, ambiguous visual match, or unexpected third PDF image blocks preparation; correct the controlled sources and rerun the builder, then validate again.

1. Create the dated project folder and save its master INDD directly in the project root.
   In this TSUM workspace, seed that master from `SOURCES\TSUM_FS-0260712_LB_LLM_01_AUTOMATION.indd`; it is the prepared front + reusable working spread + back-cover template.
2. Put every named source image in `control\work\_mat\hires`, then run `scripts\build_reference_registry.py <project>` before any map preparation or controller command. Inspect its `registry-build` contact proofs; it must write the complete photo-only `control\work\look-register.tsv` and pass `scripts\create_look_registry.py control\work\look-register.tsv --validate-ready`. For every pair, visually verify the intended presentation direction before import: the full-length photo is left and the close-up is right. Its columns must never contain an Excel row, gender-based sequence, or other credit mapping.
3. Copy the reference PDF to `control\work\_mat\reference.pdf`, pass it to `init` as `--reference`, run `prepare-reference-order`, inspect every generated `LOOK_###` source-page/hires proof card, and run one immutable `confirm-reference-look` for every look. Work in batches of at most five cards: after viewing a batch, write the confirmations for those same IDs in the **same turn**, count the confirmation files, and only then view the next batch. `validate-map` is blocked until all source-PDF comparisons are current. The register must use consecutive `pdf_spread`, InDesign pages, and look IDs; an Excel-derived order cannot be labelled as a PDF order.

   A `NEXT map` status with `REFERENCE_ORDER_CONFIRMATIONS x/N` is never a user-facing blocker when the proof cards exist: it is a precise autonomous work queue. Continue with the next five unconfirmed cards, inspect them at `high` or `original` detail, confirm those exact five in the same turn, count the immutable records only in `control\reference-order\confirmations`, and re-read status. The similarly named `control\work\reference-order` folder contains cards, not confirmations. Do not ask the user for ordering, re-run matching, or move to Excel credits until the counter reaches `N/N`.
4. Copy the source workbook inside `control\work`, run `prepare_caption_mapping.py`, then create and inspect contact sheets for `control\work\_mat\excel-images` plus the generated `control\work\mapping-review\required-pdf-looks` cards. For each already-ordered PDF look, visually compare its exact left/right hires pair against every plausible Excel card and fill one `control\work\caption-map.tsv` row. Ignore unmatched extra Excel cards. Never infer a row by look order, gender, a filename, or a nearest-image score.

   After that script finishes, use its actual generated paths exactly: individual Excel previews are `control\work\mapping-review\excel\previews\<sheet>_<look>.jpg`; Excel contact sheets are `control\work\mapping-review\excel\sheets\contact-01.jpg`, `contact-02.jpg`, and so on; required PDF pair cards are in `control\work\mapping-review\required-pdf-looks`. Never guess a shortened `mapping-review\excel\<id>.jpg` path or a non-existent `contact-04.jpg`.

   First run the supplied `auto_caption_map.py <project> --mode seed`. It performs a one-to-one, image-identity assignment without using Excel order, but this is only a proposal: **no row is automatically CONFIRMED**, including a first-rank/low-score row. Render the exact proof cards, then `confirm-strong` may only check that evidence files exist; it never changes a status. Inspect every exact three-panel card in batches of at most five. For the same viewed batch run `auto_caption_map.py <project> --mode confirm-review --looks LOOK_###,... --notes 'LOOK_###=specific visible identity cues||...'`; the command records the proof hash and the grounded observation. A generic note cannot pass. If any proposed card shows a different garment, model, colour, bag, shoes, or pose, leave it PENDING; identify a candidate from the controlled Excel previews, run `alternative-proofs --looks LOOK_###,... --assignments LOOK_###=W:12,...` only for those exact candidate cards, inspect those exact new three-panel proofs, and use the same complete PENDING swap with `select-alternatives` before regenerating the ordinary cards. Never use `confirm`, never accept a weak/second-rank candidate solely on its score, and do not overwrite a non-blank Excel mapping with `seed`.
5. Resolve the obvious cards first, then use one-to-one elimination only for the remaining cards. Work in batches of at most five looks: display the required PDF pair and its candidate Excel cards, update only that batch, and re-read the affected rows before proceeding. Do not send more than five image cards to the vision tool in one call.
6. Run `render_caption_mapping_evidence.py` to create an exact three-panel proof card for every proposed match. Inspect every card in separate batches of at most five. Change a row from `PENDING` to `CONFIRMED` only in the same batch in which its rendered three-panel card was actually viewed. Before `build_verified_caption_data.py`, re-read the map and require exactly one `CONFIRMED` row for every `LOOK_###`; never announce a visual check just because the cards were generated.
7. Generate `control\work\caption-data.tsv` and `control\work\caption-provenance.json` exclusively with `build_verified_caption_data.py`.
7. Run `init` with the master, photo registry, caption map, copied workbook, provenance, captions, hires, reference PDF, look count, requested `DD.MM.YYYY`, and exact visible date text.
8. Run `validate-map`. Do not change the INDD until it prints `PASS map`.

### Disposable staged smoke tests

Before relying on a changed bot/controller, prove the full workflow in this order: one look, five looks, then the full PDF-authoritative release. For the 1- and 5-look runs, create a fresh dated test project from the approved automation master and use only:

```powershell
& $py scripts\create_lookbook_test_fixture.py <project> --looks 1 --reference <full-reference.pdf> --hires <full-hires-folder> --workbook <full-catalogue.xlsx>
```

The fixture extracts the front cover plus the first requested PDF look pages, asks the official registry builder to select their hires, reruns that builder against the reduced set, and retains the complete Excel catalogue. It must never use a manually cropped source, a prefilled registry, or a borrowed caption map. A smoke test still executes every gate through `REVIEW READY`; it does not receive approval or final distribution PDFs. Use a new date/project for each attempt and do not start the next scale until the prior controller state and review PDF have passed.

### Non-negotiable caption map policy

The original photo registry has no authority over credits. The only authority is a one-to-one `caption-map.tsv` row whose visual proof card shows exactly: selected embedded Excel image, left placed JPG, right placed JPG. The controller independently regenerates product rows from the copied workbook and rejects captions if a single character, map hash, workbook hash, or proof-image input differs.

Do not use an order list such as `female, male, female`, an incrementing `W1/M1` counter, PDF page position, filename sorting, a similarity ranking, or a previous project registry as a caption map. These are all hard blocks, even if their output looks plausible.

A count mismatch alone is not an unresolved map. Under the Excel-completeness invariant, difficult matches must be deferred to the second pass and resolved from the shrinking unmatched pools; do not substitute a nearby/ordered card. A numerical image similarity is likewise never visual confirmation: a shared studio background can make a wrong card appear statistically close.

Native automation opens the saved master through the installed InDesign COM object model. Therefore the master must be closed in InDesign before an `apply` or `export-pdf` command. Never work around this guard; save/close the document and retry the same gate.

### Dialog hygiene — no manual acknowledgement for known opening alerts

The native opener temporarily sets InDesign's script interaction level to `NEVER_INTERACT` only while it opens a controlled master, then restores the previous setting immediately. This suppresses the standard missing-link/missing-font opening alert without a person clicking `OK`; the later visual and release audits still decide whether the saved output is usable.

Never use screen-click automation to accept an unknown dialog. Save, overwrite, relink, recovery, license, permission, and any dialog that can alter a document or external state remain hard blocks. A hung automation-owned master is recovered only through the controller's safe recovery policy; do not guess which button to press.

## Exact gate loop

For each InDesign gate (`structure`, `dates`, `frames`, `images`, `captions`, `release`), use exactly:

```powershell
& $py $gate status $project
& $py $gate arm $project --gate structure
& $py $gate apply $project --gate structure
& $py $gate status $project
```

Replace `structure` only with the next required gate. `apply` performs one transactional native InDesign operation: it creates a pre-change checkpoint, opens the closed master, changes only the armed stage, rechecks the result, saves, writes evidence, and closes the master. On failure it writes no PASS evidence and does not unlock the next stage.

For `images` and `captions`, one `apply` command performs exactly one four-look transaction and then returns a durable `CHECKPOINT` (or final `PASS`). After each batch it saves and closes InDesign, verifies the changed frames, and records `control\progress\images.json` or `control\progress\captions.json`. Call the same `apply --gate <current gate>` again only after the preceding command has returned and the checkpoint is visible; never launch a second apply after a shell timeout while the first native worker may still be running. If InDesign closes, freezes, or the task is resumed later, rerun the same `apply --gate <current gate>` without `arm`; it resumes from the saved count. Never re-arm a batched gate while its progress exists.

`apply-composition` follows the same rule: one call applies and verifies exactly one saved four-look visual batch, then returns `CHECKPOINT composition` or `PASS composition`. Repeat the identical command only after that return and its progress record are visible. Never keep a 50-look visual loop alive inside one shell command and never start a second composition worker after a timeout.

When a blocked caption-clearance proof produces a signed correction plan, the next `apply-composition` is a **delta** pass: it retains the archived, already proven 50-look native baseline, checks every unchanged link and graphic bound read-only, and applies only the changed photo crops or existing credits-frame targets in the same four-look saved transactions. Its final native evidence still contains all looks and is bound to the new plan. Never replace this with an unchecked bulk reapply or a full-document mouse operation.

On the first delta call, `control\progress\composition-delta.json` does not exist yet by design. Treat its absence before the native call as an empty zero-look checkpoint; require it only after an incomplete native return. If the worker returns without both final evidence and a checkpoint, keep the visual gate blocked with that specific recovery message.

In a multi-batch delta pass, validate only the caption-frame corrections recorded in the saved delta checkpoint after each batch. Require the complete correction set only in the final transaction; a correction scheduled for a later batch is not an error on the earlier checkpoint.

If a visual attempt stops after `composition-plan.tsv` has been written, resume that exact signed plan; never run `prepare_composition_audit.py` a second time. Before its first delta batch, `reconcile-clearance-plan-priors` may restore a prior credits-frame position only from same-master native `ApplyComposition` or `ApplyCompositionDelta` evidence. It never opens InDesign and refuses a plan with a durable delta checkpoint.

Do not use the Pages panel to duplicate spreads, create pages, drag pages, or create frames during this workflow. Do not run legacy scripts such as `Audit Lookbook Master.jsx`, `audit_lookbook_master.jsx`, or any project-copied audit. The only visual-stage exception is a recorded correction of an existing image graphic: it may exchange the two existing graphics or move a graphic horizontally inside its own existing frame, never move or alter a frame. The Scripts-panel runner beginning `00_LOOKBOOK_GATE` exists only as a diagnosed manual fallback, never as the normal route.

### Controller reconciliation after a Codex timeout

The end of an agent message, a shell timeout, or a non-zero Codex exit is **not** proof that the native InDesign child stopped. Before reporting a failure, retrying an operation, or beginning the next gate, poll `lookbook_gate.py status` and the controller evidence until the state is stable. The controller's latest `NEXT` gate is the only truth:

1. If evidence advances, immediately continue at that newly unlocked gate in the same task; do not ask the user to resend the request and do not repeat the passed gate.
2. If a `*.idlk` master-lock exists, InDesign still owns the document. Wait for it to disappear and for the evidence files to settle; never start a second `apply` or a second agent turn against that master.
3. If the lock persists beyond the automation wait limit without a new PASS evidence file, treat the native action as hung: keep the controller at its current gate and stop automatic continuations/recovery. Do not turn a stale lock into a reason to start a second operation.
4. Only when no evidence has advanced, no lock remains, and the recorded error is stable may one recovery attempt diagnose and rerun the same current gate.
5. A controller state that moves from `NEXT structure` to `NEXT dates` is a successful structure stage even if an earlier agent report still says structure. The stale report must never override evidence.

### Safe recovery of a stalled visual-proof export

The visual-proof export has a shorter controlled timeout. On timeout, the controller may restart InDesign **only** when all of the following are true: the controlled master has no `*.idlk` lock; exactly one InDesign instance exists; its visible title is the neutral `Adobe InDesign 2026` start window (not a document); and its executable path is known. It terminates the timed-out worker, restarts that same instance hidden, and leaves the gate locked for one retry of the same visual-proof command. If any condition is not provable, it must leave InDesign untouched and report the block; never close a potentially user-owned document.

## What each native gate may change

- `structure`: from the prepared four-page template, duplicate the complete existing working spread before the back cover. The result must be: front cover, consecutive two-page working spreads, back cover. No pages may follow the back cover.
- `dates`: change only the existing `LOOKBOOK_SHOW_DATE` range and the date range inside existing `LOOKBOOK_LEGAL`; the legal text outside the date is preserved exactly. The legal date is 14 calendar days later unless `--legal-offset-days` says otherwise.
- `frames`: relabel the existing left image, right image, and credits containers for each look. It must never add, delete, resize, or thread a frame.
- `images`: replace only the graphics inside exact `LOOKBOOK_LEFT_IMAGE|LOOK_###` and `LOOKBOOK_RIGHT_IMAGE|LOOK_###` containers; each source filename must equal the frozen registry row. Place directly into the existing frame so InDesign replaces its graphic; never delete the existing graphic before placing the replacement.
- `captions`: write verified tabular product data only into the existing exact credits frame for its look. Before changing its story, turn off the template's width-and-height auto-sizing and restore the frame's captured four coordinates; verify that its top, left, and right coordinates are unchanged before inserting text. Otherwise InDesign can narrow the column as a side effect of the first story rewrite. For each product write the four fields tab-delimited, Find/Change every tab to a paragraph return inside that frame, apply the exact `CREDiTs` paragraph style to every resulting paragraph, and explicitly apply the template's existing `CATEGORY`, `BRAND`, `PRICE`, and `SKU` character styles in that field order. Do not rely on the GREP-style fallback: a numeric price can also match the brand expression. The first product field must start at the frame's normal top inset: set vertical justification to top and reject a leading empty paragraph or tab. Never compact local type, leading, tracking, or product spacing. If correct credits overflow, grow the **same** credits frame only by its bottom edge, only as far as the page bottom; keep its top, left, and right edges fixed. Never add, move, widen, narrow, or thread a credits frame. Record the exact controlled credits geometry after the native transaction; hard-fail if it still overflows.
- `visual`: first inspect the current spreads and write `control\visual\composition-plan.tsv`. Its sources name the intended graphics in the fixed containers and its signed shift moves only the placed left graphic, never a frame. Run `apply-composition`; the native worker first restores the placed left graphic to its standard fill in the same fixed frame, then applies the plan's **absolute** horizontal offset exactly once. This reset is mandatory on every retry: offsets must never accumulate across visual iterations. The worker records every source, graphic bounds before/after, and a structure-baseline check in saved four-look batches. Then run `render-visual-proof`: it exports the current saved INDD, renders one exact two-page proof image per look, and runs the mandatory caption-clearance audit. It owns a durable export marker while InDesign is working. If a shell wrapper times out, wait for that same marker/lock and inspect the existing proof; never start another visual export in parallel. That audit checks the actual fixed-frame overflow and maps each visible PDF credit block back to the original placed left photo; it derives a per-row studio-background surface from both source edges (not one global grey), removes isolated JPEG speckle, and records an overlay in `control\visual\caption-clearance` for every look. `CLEAR` is the only passing result. `OVERFLOW` returns to the captions gate. For `COLLISION` or `REVIEW`, run `plan-clearance-corrections`: it must first exhaust the bounded **horizontal** travel of the placed left graphic at full source resolution. If and only if no such crop is `CLEAR`, it may move the **same existing** credits frame down inside its own left page; if that fails, it may move that same frame to the right side of the page, right-align every `CREDiTs` paragraph, and may lower it there when required to obtain a fully clear rectangle. If neither change alone is clear, it must additionally test their **combination**: a bounded horizontal move of the placed left graphic together with a downward or right-and-downward move of that same existing credits frame. Credits must remain inside the page-margin safe zone **with a 12-pt internal gutter**; aligning them directly to the guide is not permitted. The test uses the rendered glyph bounds, not harmless empty space inside the text frame. It must preserve the frame's dimensions, text, style, parent page and top vertical alignment; it must not create, delete, duplicate, thread, resize, narrow or widen a credits frame. Every permitted target has signed before/after geometry, is rechecked natively, and is carried into later retry plans. Never write a "best" position that remains blocked. If one known look needs calibration, use `calibrate-safe-area --look LOOK_###`, then `apply-safe-area-calibration --look LOOK_###` and `render-safe-area-preview --look LOOK_###`; this changes and verifies only that spread before any full 50-look proof is repeated. If no permitted image or credits-frame position is `CLEAR`, record that look in `unresolved` and stop at the visual gate without export. It never moves a graphic vertically. Do not create any visual confirmation while caption-clearance is blocked. After every result is `CLEAR`, inspect every actual proof pair and run `confirm-visual-look` once for each `LOOK_###`; its non-overwritable record binds the specific observation and clearance result to that proof-pair hash. Only then run `record-visual`. A TSV entry, copied mapping card, generic screenshot, or bulk status rewrite is never visual proof.
- `release`: no edit. Native checks re-run structure, baseline frame counts, dates, labels, links, captions, and current visual proof together.

The structure gate records the page-item, text-frame, and geometry baseline of every labelled left image, right image, and credits frame. Any later added/deleted object or moved/resized container is a hard failure, except for the controlled credit-frame bottom-edge expansion recorded by the captions transaction and a signed visual-clearance target for the same existing credits frame. That target may only be downward, or rightward (with an optional downward component); it is checked against both the saved captions geometry and the native evidence before every later gate. InDesign may round persisted bounds by up to 0.24 pt across COM sessions; use a 0.5-pt comparison tolerance, never a looser visual guess. A composition correction may move a placed graphic only horizontally inside its own frame.

### Centered crop rule — mandatory visual planning

The actual printed credit glyphs, not the unused rectangle of their text frame,
are the only pixels that may drive a model-clearance decision. Derive the
visible text block from the rendered PDF on both axes and retain a conservative
glyph-width allowance; never treat blank frame space as a collision.

The normal photo crop is centred. A clearance correction may move the placed
photo horizontally by no more than **36 pt** from that centred fill. Do not
push a model to, or beyond, a page edge merely because a wide credits frame has
empty space. If printed credits cannot become clear within that bounded crop,
try the already permitted existing credits-frame locations (down first, then
right/down with right-aligned text) and their bounded combination. Keep the
credits frame inside the safe area.

## Export lock

Only when `status` prints `NEXT pdf`:

```powershell
& $py $gate pre-export $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf' --quarantine-existing
& $py $gate export-pdf $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf'
& $py $gate verify-pdf $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf'
& $py $gate complete $project
```

`export-pdf` uses InDesign’s native Interactive PDF export at 120 ppi as one multi-page PDF with reader spreads disabled. Do not open, change, or save the master between `pre-export` and `verify-pdf`. `verify-pdf` parses the permitted PDF, requires its exact page count, and renders samples. `complete` establishes a controlled review state; final delivery requires later explicit approval and `publish-final`.

## Review, revisions, and approval

`verify-pdf` is a full order audit, not a page-count check: it compares every exported left/right look page to the current InDesign proof, which is itself bound to the individually confirmed PDF-reference order. A reordered, substituted, or stale review PDF must fail before `complete`.

The 120-ppi PDF made by `pre-export` → `export-pdf` → `verify-pdf` is the **review PDF**, not the final distribution set. After it passes, run `complete`: it means **REVIEW READY**, then wait for the human review. Do not create compression variants yet and do not merely reply that the lookbook is approved.

When review corrections arrive, they must identify the affected pages. Record that request and run `begin-revision --notes '…'`. It copies the reviewed `*_01.indd` into the next sequential `*_02.indd` (then `*_03.indd`, and so on), preserves the previous proof in history, and returns the new master to the visual-review gate. Apply the received changes only to the new master; then repeat the composition plan, native application, current-master pair proof, individual visual confirmations, release audit, and 120-ppi review-PDF verification. A correction that changes approved source images or credits must be returned through the corresponding controlled source stage rather than hand-edited.

A user message `согласовано`, `согласован`, or `approved` after `REVIEW READY` is an approval trigger, not merely a conversational acknowledgement. Immediately run:

```powershell
& $py $gate publish-final $project --approval-note 'согласовано'
```

It writes and parses all five required final PDFs from the approved, unchanged master. It never overwrites an existing output and resumes safely if a previous final-export attempt stopped between variants:

| Output | Export resolution | Exact path |
| --- | --- | --- |
| General | 120 ppi | `<master>_10mb.pdf` |
| General | 220 ppi | `<master>_20mb.pdf` |
| General | 300 ppi | `<master>_40mb.pdf` |
| Men only | 300 ppi | `Gender\<master>_M.pdf` |
| Women only | 300 ppi | `Gender\<master>_W.pdf` |

`<master>` is the final INDD filename without `.indd`, for example `TSUM_FS-0260726_LB_WA_01`. The two gender PDFs contain only the corresponding two-page look spreads, selected from the verified `M`/`W` caption map; they do not include the common front or back cover. The suffixes `_10mb`, `_20mb`, and `_40mb` identify the required 120/220/300-ppi variants; the controller records actual file identities and page counts rather than pretending to guarantee an exact byte size.

## Recovery

- Treat an InDesign error, open-master guard, bad source file, failed check, or incomplete final-output set as work at the current gate — never as permission to skip ahead or send a final answer.
- The checkpoint in `control\checkpoints\<gate>-before.indd` lets the operator restore the exact pre-gate master if a failed external condition requires manual diagnosis. It is not a later-stage source file.
- If the template has not been prepared for native duplication, use `run_lookbook_gate_com.ps1 -Action PrepareTemplate` on a copy only. Never modify a production master to “make it compatible” without first preserving it.
- Do not report partial structure as a finished lookbook. Keep the task active until `complete` passes for review, then until `publish-final` prints `FINAL DELIVERABLES PASS` after user approval.

### COM disconnect recovery

`RPC_E_DISCONNECTED` / `0x80010108` is a broken temporary COM client, not proof that the lookbook is invalid. The controller may restart InDesign and retry the same armed command exactly once only when exactly one InDesign process shows the neutral start window with no document. If the crash has already terminated every InDesign process, its project-local `.idlk` is stale by definition: remove only that project lock, launch the known InDesign 2026 executable, and retry once. A visible document, multiple processes, or an unknown executable remains a hard stop; never re-arm or skip the gate.

### Visual-review feedback

For a full visual gate, report durable `confirmed / total` progress from `control/visual/confirmations`, rather than elapsed time. `50/50` can be visible briefly while immutable `PASS visual` evidence is being written.

After the proof PDF and caption-clearance audit are frozen, visual inspection may run in up to four independent workers. Give every worker a disjoint fixed assignment of at most five `LOOK_###` proof pairs. A worker may only inspect its assigned files and write `confirm-visual-look` for those exact IDs; it must not invoke InDesign, `arm`, composition, rendering, export, or `record-visual`. The coordinator waits for every worker, verifies one immutable confirmation per expected look, then alone runs `record-visual`. Proof generation, any native correction, and final PASS remain single-writer operations.

## Verified InDesign 2026 COM rules

- A master already open in InDesign is a hard stop for native work. Close it; never race the UI or attach to its unsaved state.
- In the legal text, PowerShell cannot assign `Characters.ItemByRange(...).Contents` even though InDesign advertises it. Change only the ten existing date characters by their one-based character positions and compare the full legal text before/after.
- `InteractivePDFExportPreferences.ExportAsSinglePages` must be `false`. With `true`, InDesign creates a folder containing separate PDFs and violates the single permitted-PDF path.
- Native checkpoints and PASS evidence are generated before a later gate unlocks. A COM return value alone is never success.
- Image and caption batches contain four looks, are saved and rechecked separately, and retain the first pre-batch checkpoint. Each `apply` command returns after one such batch, so wait for its `CHECKPOINT` or `PASS` before issuing another call; a tool-wrapper timeout is not permission to start a parallel transaction. `IMAGE_PROGRESS x/n` and `CAPTION_PROGRESS x/n` are recovery states, never reasons to stop or re-arm.
- The prepared template's credit style is case-sensitive: `CREDiTs`, not `CREDITS`. A caption gate fails if a tab remains or any credit paragraph has another style.
- `CREDiTs` contains GREP formatting, but GREP is not sufficient proof of the four product fields: after Find/Change, apply and verify `CATEGORY`, `BRAND`, `PRICE`, and `SKU` explicitly. Verify each field's point size, leading, and any explicitly set tracking against its own character style; `NothingEnum` means inherited and must not be compared to a literal number.
- A short credits block must never be vertically centered in a tall container. Its frame must use top vertical justification and begin with text rather than an empty paragraph. Disable width-and-height auto-sizing **before** changing the credit story, then restore and recheck the existing frame's original bounds; disabling it after the rewrite is too late because InDesign may already have narrowed the column. When correct credits need more room, enlarge only the existing frame's bottom edge, then store its exact geometry in `evidence/caption-geometry.json` before visual proof. The planner uses the actual visible text block for collision pixels but moves the full existing frame bounds, never the smaller visible bounding box.
- InDesign can drop COM while editing many text stories or leave a saved master open. Process caption repair in four-look saved batches; resume only from a durable progress record. Automatically close the exact saved controlled master before the next batch, never discard a user document with unsaved changes. If native text preferences disconnect after a checkpoint, restart only a document-free automation instance and continue from that checkpoint.
- The composition plan is not a confirmation: it has one ordered row per required look and can name only the two source images registered for that look. The native worker applies it only inside the existing fixed containers. A separate signed clearance plan may carry only exact corrections to pre-existing credits frames; `RIGHT` requires right paragraph alignment and both modes retain top vertical alignment. Release validates those actual links and correction coordinates; visual release additionally requires current-master rendered proof and one non-overwritable confirmation for every look.

For commands, TSV column rules, screenshot names, and the initialization example, use `references/gated-control.md`.
