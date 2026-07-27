# Evidence-gated native lookbook release

`lookbook_gate.py` is the authority for every production lookbook. It calls `run_lookbook_gate_com.ps1`, which controls InDesign through its installed native COM object model. This removes slow mouse-driven duplication and prevents a saved screen state from being mistaken for a completed stage.

## One-time setup

Set the project paths in PowerShell:

```powershell
$py = 'C:\Users\vdiza\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$gate = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\lookbook_gate.py'
$project = 'C:\Users\vdiza\Desktop\TSUM\2026\Lookbook\TSUM_FS-0260726'
```

The saved master `.indd` must sit directly in `$project`. The PDF reference determines every required look and its order; build the photo registry from that reference before opening Excel. The registry binds only photo pairs to pages; credits are not allowed in it. Excel is a catalogue and may legitimately have unused extra looks — its number of cards must not be compared to the PDF count. Before map validation, create the reviewed Excel-to-photo map and its proof cards:

For this TSUM workspace, create the project master as a copy of:

`C:\Users\vdiza\Desktop\TSUM\2026\Lookbook\SOURCES\TSUM_FS-0260712_LB_LLM_01_AUTOMATION.indd`

It has the generic reusable work labels required by native duplication; leave the approved source template unchanged.

```powershell
$mappingPrep = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\prepare_caption_mapping.py'
$mappingProof = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\render_caption_mapping_evidence.py'
$captionBuild = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\build_verified_caption_data.py'
$workArea = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\create_lookbook_work_area.py'
$registryBuild = 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\build_reference_registry.py'

& $py $workArea $project
# Copy reference.pdf and all hires into control/work/_mat first, then build the complete registry.
# Do not use create_look_registry.py --count in a live project: it is a deliberately incomplete fixture stub.
& $py $registryBuild $project
& $py 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\create_look_registry.py' "$project\control\work\look-register.tsv" --validate-ready
& $py $mappingPrep $project --workbook 'C:\Users\vdiza\Desktop\TSUM\2026\Lookbook\SOURCES\_mat\FS 24.05.xlsx'
& $py 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\build_hires_previews.py' "$project\control\work\_mat\excel-images" "$project\control\work\mapping-review\excel"
# Map and verify no more than five looks in one visual batch. Map the obvious cards first, then resolve only the shrinking unmatched pool; never submit all 50 proof cards to one vision call.
# First pass: in PDF order, map every obvious control\work\mapping-review\required-pdf-looks\LOOK_###.jpg ↔ Excel-card match and leave its visual_status as PENDING. Extra Excel cards remain unused.
# Second pass: compare only the remaining PDF pairs and remaining Excel cards; resolve every deferred row by detailed visual evidence and elimination. Every required PDF look has an Excel match, so do not report an “unmatched” look or ask the user for a table.
# Then render proof cards and change every visual_status to CONFIRMED. Identify from model, garments, accessories, shoes, bag, pose/crop — never position, gender, W/M counters, or filename order.
& $py $mappingProof $project --map 'control/work/caption-map.tsv' --hires 'control/work/_mat/hires'
# Inspect control\work\mapping-evidence\LOOK_###.jpg in calls of at most five cards. Only set the same viewed batch to CONFIRMED.
# At the end, count exactly 50 CONFIRMED and zero PENDING statuses with no duplicate Excel sheet/look pair. A generated card or contact sheet alone is not confirmation.
& $py $captionBuild "$project\control\work\caption-map.tsv" "$project\control\work\_mat\caption-source.xlsx" "$project\control\work\caption-data.tsv" --provenance "$project\control\work\caption-provenance.json"
```

The controller independently regenerates credits from the copied workbook during `validate-map`; a guessed Excel row, stale source, or hand-edited credit text blocks the release.

```powershell
& $py $gate init $project `
  --master 'TSUM_FS-0260726_LB_WA_01.indd' `
  --registry 'control/work/look-register.tsv' `
  --captions 'control/work/caption-data.tsv' `
  --caption-map 'control/work/caption-map.tsv' `
  --caption-workbook 'control/work/_mat/caption-source.xlsx' `
  --caption-provenance 'control/work/caption-provenance.json' `
  --hires 'control/work/_mat/hires' `
  --reference 'control/work/_mat/reference.pdf' `
  --looks 50 `
  --show-date '26.07.2026' `
  --show-text '26 ИЮЛЯ'
```

For an intentional fresh session use the same command with `--restart`. It archives prior evidence; it never reuses it.

Before Excel credit mapping can be accepted, bind the exact reference pages to the photo-only registry. This is a separate required visual proof: it does not use Excel row numbers, gender order, or filenames as an order surrogate.

```powershell
& $py $gate prepare-reference-order $project
# Inspect every control\work\reference-order\rendered\<timestamp>\cards\LOOK_###.jpg.
# Each card shows the source PDF spread over the exact registered left/right hires pair.
& $py $gate confirm-reference-look $project --look LOOK_001 --note 'Reference spread matches this registered pair: full length left and close-up right.'
# Repeat the one-look command after inspecting every LOOK_###. Confirmations are immutable and hash-bound to the source PDF and registry.
```

`validate-map` blocks unless all reference-page comparisons are present and current. The register requires `pdf_spread` 1..N and fixed InDesign pairs 2/3, 4/5, ...; Excel cannot silently replace the reference sequence.

Check the only durable status with:

```powershell
& $py $gate status $project
```

## Required sequence

| Gate | Action | Evidence |
| --- | --- | --- |
| `map` | Complete and reconcile the TSV registry; do not change InDesign. | `validate-map` |
| `structure` | Native duplicate of whole working spreads before the back cover. | `arm` → `apply` |
| `dates` | Native update of only marked date ranges. | `arm` → `apply` |
| `frames` | Native binding of existing frames to exact look labels. | `arm` → `apply` |
| `images` | Native replacement of contents in existing exact frames. | `arm` → `apply` |
| `captions` | Native update of existing exact credit frames. | `arm` → `apply` |
| `visual` | Native composition application, current-INDD pair proof, then one confirmation per look. | `arm` → plan → apply → render → inspect/confirm all → `record-visual` |
| `release` | Native read-only final audit. | `arm` → `apply` |
| `pdf` | Native Interactive PDF export, parse, render samples. | permit → export → verify → complete |

For every native InDesign gate, first save and close the master in InDesign, then run:

```powershell
& $py $gate arm $project --gate structure
& $py $gate apply $project --gate structure
& $py $gate status $project
```

No other gate is unlocked until `apply` writes current PASS evidence. If it fails, do not arm a later gate: correct input or the current saved master, then rerun that same gate.

### Caption formatting contract

The native caption step is a Find/Change operation inside each existing `LOOKBOOK_CREDITS|LOOK_###` frame: four tab-separated fields are written, every tab becomes a paragraph return, and every resulting paragraph receives the exact case-sensitive style `CREDiTs`. The gate fails on a remaining tab, another paragraph style, a threaded frame, or a newly created frame. `normalize_lookbook_credits.jsx` is only a diagnosed manual fallback and uses the same `CREDiTs` name.

### Batched image and caption recovery

The image and caption stages are split into four-look transactions. After every transaction the worker saves and closes InDesign, verifies the exact changed frames, and writes `control\progress\images.json` or `control\progress\captions.json`. `status` prints `IMAGE_PROGRESS x/n` or `CAPTION_PROGRESS x/n` while the gate remains locked.

If InDesign stops or is restarted, close any copy of the master and run only:

```powershell
& $py $gate apply $project --gate <current gate>
```

Do **not** run `arm` for the current batched gate again. A new arm would discard the nonce that binds saved recovery progress to the current session. The controller resumes the first incomplete look and grants PASS only after every image link or credit frame is verified.

## Source labels and template requirements

The template needs existing objects marked with:

- `LOOKBOOK_FRONT` on the first page;
- `LOOKBOOK_BACK` and `LOOKBOOK_LEGAL` on the final page;
- `LOOKBOOK_SHOW_DATE` on the front;
- on the one reusable working spread: `LOOKBOOK_LEFT_IMAGE`, `LOOKBOOK_RIGHT_IMAGE`, `LOOKBOOK_CREDITS`.

The `structure` gate uses that generic working spread to make all required working spreads before the back cover. The `frames` gate converts every generic label into its exact `|LOOK_###` form. No stage creates new containers.

To prepare an isolated automation copy from an approved four-page source template:

```powershell
& 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\run_lookbook_gate_com.ps1' `
  -Action PrepareTemplate `
  -TemplateSource 'C:\path\to\source.indd' `
  -TemplateDestination 'C:\path\to\prepared-template.indd'
```

It refuses to overwrite a destination and changes only the three work labels on the copy.

## Visual proof

After captions pass:

```powershell
& $py $gate arm $project --gate visual
& $py 'C:\Users\vdiza\.codex\skills\lookbook-layout\scripts\prepare_composition_audit.py' $project
& $py $gate apply-composition $project
& $py $gate render-visual-proof $project
```

Inspect **every** image in `control\visual\proof\pairs\<timestamp>\LOOK_###.jpg`. These images are rendered from the current saved INDD through the native PDF exporter; mapping cards, copied screenshots, or JPGs from another source never qualify. Before the render, `control\visual\composition-plan.tsv` must contain one row per look:

| Column | Required value / meaning |
| --- | --- |
| `orientation` | `FULL_LEFT_CLOSE_RIGHT` — full-length image left, close-up right. |
| `left_image_filename` / `right_image_filename` | The intended source file in the fixed left / right container. They can contain only the two files registered for this look. |
| `photo_adjustment_points` | Signed horizontal move of the placed **left** image content; `0` when unchanged, positive right, negative left. |
| `plan_status` | `READY` only. This is a correction plan, not an attestation. |

For a reversed pair, write the full-length source photo in the plan's existing left container and the close-up source photo in its existing right container; do not exchange pages or frames. When credits touch the model, state the signed crop shift in the plan. `apply-composition` performs the replacement/movement only inside fixed containers, then writes `composition-applied.json` with every graphic's actual bounds before and after. It rejects a moved or resized frame and resumes in saved four-look batches after an InDesign restart.

After `render-visual-proof`, use one command for every proof image immediately after its actual inspection; the command is deliberately one-look-only and refuses an overwrite:

```powershell
& $py $gate confirm-visual-look $project --look LOOK_001 --note 'Кредиты слева не пересекают фигуру; полный рост слева, клоузап справа.'
```

Do this for every `LOOK_###`, then record the visual gate:

```powershell
& $py $gate record-visual $project --notes 'Проверены все текущие рендеры разворотов: полный рост слева, клоузап справа, кредиты не пересекают модель.'
```

### Targeted safe-area calibration

When one identified look has a credits-frame issue, do not rerun the full 102-page proof merely to test a correction. The planner must keep the same existing frame inside the page margins with a 12-pt internal gutter, then apply and inspect that exact two-page spread:

```powershell
& $py $gate calibrate-safe-area $project --look LOOK_035
& $py $gate apply-safe-area-calibration $project --look LOOK_035
& $py $gate render-safe-area-preview $project --look LOOK_035
```

The targeted preview is stored under `control\visual\calibration-previews`; it verifies the saved master, visible credit fields, safe-area geometry, and source-pixel clearance. It is calibration evidence only. After the correction is accepted, the ordinary full visual-proof cycle is still required before release.

The controller rejects `record-visual`, release, and PDF export unless all of these are current and hash-bound to the same saved master: the plan, native application log, native proof PDF, all pair renders, five overview renders, the caption-clearance report, and exactly one individual confirmation per required look. Do not hand-write or mass-rewrite `composition-applied.json`, proof manifests, caption-clearance evidence, or confirmation files; only the controller commands may create them.

### Mandatory caption-clearance audit

`render-visual-proof` also runs a non-skippable computer check before any `confirm-visual-look` command is accepted. It reads the real saved InDesign bounds of the left graphic and fixed credits frame, takes the **visible credit-block bounds from the current proof PDF**, maps those blocks to the original placed source photo, and writes one red/green evidence image per look in `control\visual\caption-clearance\<timestamp>`. It also checks `TextFrame.Overflows` and the visible product-block count.

- Only `CLEAR` allows visual confirmation and release.
- `OVERFLOW` means clipped or missing credit text. Return to the captions gate; a crop shift never fixes it.
- `COLLISION` means model or garment pixels occur under a visible credit block. Set a signed horizontal `photo_adjustment_points` value, apply the composition, and regenerate the proof. Never move a frame or vertically move a graphic.
- `REVIEW` is intentionally a block: the pixels are not provably background. Rework the crop horizontally and regenerate the proof.

The controller binds each later individual confirmation and the visual evidence to the exact audit report hash. A generic note, a quickly created batch of confirmations, or a prior audit cannot override this check.

## Controlled export

When and only when status is `NEXT pdf`:

```powershell
& $py $gate pre-export $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf' --quarantine-existing
& $py $gate export-pdf $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf'
& $py $gate verify-pdf $project --pdf 'TSUM_FS-0260726_LB_WA_01_review.pdf'
& $py $gate complete $project
```

`verify-pdf` rejects an absent, malformed, stale, wrong-page-count, or unpermitted PDF and saves rendered review samples under `control\visual\pdf`. It additionally compares every exported look page to the current InDesign proof at the same PDF-reference position; a page-order change, swapped spread, or substituted export blocks `complete`. `complete` prints `REVIEW READY`: it is the controlled PDF for human review, not the final distribution set.

## Review corrections and final publication

`complete` after `verify-pdf` means `REVIEW READY`: it is the controlled PDF for human review, not the final distribution set.

When people return page-specific corrections, do not overwrite the reviewed INDD. Create the next sequential revision and record the received request:

```powershell
& $py $gate begin-revision $project --notes 'Стр. 12: …; стр. 37: …'
```

For `TSUM_FS-0260726_LB_WA_01.indd`, this creates `TSUM_FS-0260726_LB_WA_02.indd`, archives the prior visual/release/review evidence, and returns the controller to `NEXT visual`. Apply the recorded corrections only in the new master, then repeat visual → release → review PDF. Never overwrite `_01.indd` or skip the renewed visual/release checks.

Only after an explicit human message `согласовано` / `approved`, publish the final set:

```powershell
& $py $gate publish-final $project --approval-note 'согласовано'
```

The command writes and verifies exactly these files for master `TSUM_FS-0260726_LB_WA_01.indd`:

| File | ppi | Contents |
| --- | --- | --- |
| `TSUM_FS-0260726_LB_WA_01_10mb.pdf` | 120 | Full lookbook |
| `TSUM_FS-0260726_LB_WA_01_20mb.pdf` | 220 | Full lookbook |
| `TSUM_FS-0260726_LB_WA_01_40mb.pdf` | 300 | Full lookbook |
| `Gender\TSUM_FS-0260726_LB_WA_01_M.pdf` | 300 | Male two-page look spreads only |
| `Gender\TSUM_FS-0260726_LB_WA_01_W.pdf` | 300 | Female two-page look spreads only |

The gender split is built from the verified `excel_sheet` (`M` / `W`) in `caption-map.tsv`; extra Excel looks are not included. Existing final files are never overwritten. If the five-file export is interrupted, rerun the same command: checked variants are retained and only missing variants continue.

## Installation test

Before first use, run:

```powershell
& $py $gate self-test
```

This isolated controller test does not touch a lookbook or InDesign. The skill’s live native automation test is run separately only on a disposable copy of an approved template.
