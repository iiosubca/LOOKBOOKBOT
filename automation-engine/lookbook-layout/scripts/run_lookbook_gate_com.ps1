[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('ApplyGate', 'ApplyComposition', 'ApplyCompositionDelta', 'ApplyLookCalibration', 'ReapplyComposition', 'RepairCaptions', 'AuditCaptionClearance', 'RebindRevisionStructureEvidence', 'ExportPdf', 'ExportPdfSet', 'PrepareTemplate')][string]$Action,
    [string]$Project,
    [ValidateSet('structure', 'dates', 'frames', 'images', 'captions', 'release')][string]$Gate,
    [string]$Pdf,
    [string]$ExportPlan,
    [ValidateRange(72, 600)][int]$RasterResolution = 120,
    [string]$PageRange = 'ALL',
    [string]$TemplateSource,
    [string]$TemplateDestination,
    [string]$LookId,
    [ValidateRange(1, 10)][int]$BatchSize = 4
)

$ErrorActionPreference = 'Stop'

# Values come from the installed InDesign 2026 COM type library.
$LOC_BEFORE = 1650812527
$FIT_FILL_PROPORTIONALLY = 1718185072
$SAVE_NO = 1852776480
$ALL_PAGES = 1886547553
$PRINT_PDF = 1952403524 # ExportFormat.PDF_TYPE
# Adobe PDF (Print) export constants from the installed InDesign 2026 COM
# type library.  Interactive PDF has a separate raster path and does not
# control the downsampling of placed catalogue photography.  The final
# lookbook must instead use the regular Adobe PDF export preferences below.
$BITMAP_COMPRESSION_JPEG = 1785751398 # BitmapCompression.JPEG
$COMPRESSION_QUALITY_MEDIUM = 1701727588 # CompressionQuality.MEDIUM
$BICUBIC_DOWNSAMPLE = 1650742125 # Sampling.BICUBIC_DOWNSAMPLE
$MAX_COMPOSITION_SHIFT_POINTS = 36.0      # retain the centred editorial crop
$LINK_NORMAL = 1852797549
$TOP_ALIGN = 1953460256 # VerticalJustification.TOP_ALIGN
$RIGHT_ALIGN = 1919379572 # Justification.RIGHT_ALIGN
$OVERRIDE_ALL = 1634495520 # OverrideType.ALL
$NOTHING = 1851876449 # NothingEnum.NOTHING / inherit from parent style
$GEOMETRY_EPSILON = 0.5 # InDesign can round persisted frame bounds by ~0.24 pt across COM sessions
$SAFE_AREA_INTERIOR = 12.0 # keep corrected credits visibly inside the magenta safe guide
# InDesign's UserInteractionLevels.NEVER_INTERACT enum.  Keep the scope to
# document opening: it suppresses the benign missing-font/link warning without
# hiding later validation failures or changing a user's normal UI settings.
$NEVER_INTERACT = 1699640946

function Fail([string]$Message) { throw $Message }
function Read-Json([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "Missing file: $Path" }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Write-Json([string]$Path, $Value) {
    $folder = Split-Path -Parent $Path
    [System.IO.Directory]::CreateDirectory($folder) | Out-Null
    $json = $Value | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}
function Normalize-Path([string]$Path) { return [System.IO.Path]::GetFullPath($Path).ToLowerInvariant() }
function Open-AutomationDocument($Application, [string]$Path) {
    $previousInteraction = $null
    try {
        $previousInteraction = [int]$Application.ScriptPreferences.UserInteractionLevel
        $Application.ScriptPreferences.UserInteractionLevel = $NEVER_INTERACT
        return $Application.Open($Path, $false)
    } finally {
        # NEVER_INTERACT is process-global. Always restore it immediately so
        # an operator's later manual InDesign work keeps its normal alerts.
        if ($null -ne $previousInteraction) {
            try { $Application.ScriptPreferences.UserInteractionLevel = $previousInteraction } catch {}
        }
    }
}
function Close-SavedAutomationMaster($Application, [string]$MasterPath, [string]$Context) {
    # A disconnected COM client can leave the controller's own saved document
    # open. Close only that exact saved master before a new native transaction;
    # never discard an operator's unsaved work.
    $matches = @()
    foreach ($openDocument in $Application.Documents) {
        if ((Normalize-Path ([string]$openDocument.FullName)) -eq (Normalize-Path $MasterPath)) { $matches += $openDocument }
    }
    foreach ($openDocument in $matches) {
        if (-not $openDocument.Saved) { Fail "$Context found the master open with unsaved changes; automatic close is unsafe." }
        try { $openDocument.Close($SAVE_NO) } catch { Fail "$Context could not close the saved automation master." }
    }
}
function Master-Identity([string]$Path) {
    $file = Get-Item -LiteralPath $Path -ErrorAction Stop
    return [ordered]@{
        path = $file.FullName
        name = $file.Name
        length = [int64]$file.Length
        modified_ms = [int64]([DateTimeOffset]$file.LastWriteTimeUtc).ToUnixTimeMilliseconds()
    }
}
function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "Missing file for SHA-256: $Path" }
    return ([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.IO.File]::ReadAllBytes($Path)) | ForEach-Object { $_.ToString('x2') }) -join ''
}
function Same-Identity($Left, $Right) {
    return (Normalize-Path ([string]$Left.path)) -eq (Normalize-Path ([string]$Right.path)) -and
        ([int64]$Left.length -eq [int64]$Right.length) -and
        ([int64]$Left.modified_ms -eq [int64]$Right.modified_ms)
}
function Read-Tsv([string]$Path, [string[]]$ExpectedHeader) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "Missing TSV: $Path" }
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8).TrimStart([char]0xFEFF)
    $lines = $raw -split "`r?`n"
    if ($lines.Count -lt 2) { Fail "TSV has no data rows: $Path" }
    $header = $lines[0].Split("`t")
    if (($header -join '|') -ne ($ExpectedHeader -join '|')) { Fail "Unexpected TSV columns in $Path" }
    $rows = @()
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if (-not $lines[$i]) { continue }
        $cells = $lines[$i].Split("`t")
        if ($cells.Count -ne $ExpectedHeader.Count) { Fail "Malformed TSV row $($i + 1) in $Path" }
        $row = [ordered]@{}
        for ($j = 0; $j -lt $ExpectedHeader.Count; $j++) {
            $value = $cells[$j].Trim()
            if (-not $value) { Fail "Empty TSV field at row $($i + 1) in $Path" }
            $row[$ExpectedHeader[$j]] = $value
        }
        $rows += [pscustomobject]$row
    }
    return $rows
}
function Get-DocumentPageItems($Document) {
    # Do not enumerate Document.AllPageItems directly.  On a large document
    # the COM enumerable proxy can disconnect although the InDesign process
    # and the saved document are both still healthy (RPC_E_DISCONNECTED).
    # Indexed collection access uses InDesign's native collection API and is
    # stable across 100+ page releases.  Keep the returned objects unchanged:
    # callers still validate the exact labelled template frames.
    try {
        $collection = $Document.AllPageItems
        $count = [int]$collection.Count
    } catch {
        Fail 'InDesign cannot read the document page-item collection.'
    }
    $items = @()
    for ($index = 1; $index -le $count; $index++) {
        try {
            $items += $collection.Item($index)
        } catch {
            Fail "InDesign cannot read page item $index of $count."
        }
    }
    return $items
}
function Get-DocumentPageItemCount($Document) {
    try { return [int]$Document.AllPageItems.Count } catch { Fail 'InDesign cannot count document page items.' }
}
function Get-LabeledItems($Document, [string]$Label) {
    $result = @()
    foreach ($item in @(Get-DocumentPageItems $Document)) {
        if ([string]$item.Label -eq $Label) { $result += $item }
    }
    return $result
}
function Get-OneItem($Document, [string]$Label) {
    $items = @(Get-LabeledItems $Document $Label)
    if ($items.Count -ne 1) { Fail "${Label}: expected exactly one item, found $($items.Count)." }
    return $items[0]
}
function Get-ItemPage([object]$Item) {
    if ($null -eq $Item.ParentPage) { return 0 }
    $name = [string]$Item.ParentPage.Name
    if ($name -notmatch '^\d+$') { Fail "Labelled item is on a non-numeric page: $name" }
    return [int]$name
}
function Get-Text([object]$Frame) {
    try { return [string]$Frame.ParentStory.Contents } catch { Fail "$($Frame.Label): not a text frame." }
}
function Normalize-Text([string]$Text) {
    $value = $Text -replace "`r`n", "`n" -replace "`r", "`n" -replace "`t", "`n"
    # InDesign's GREP/typographer may substitute straight apostrophes with
    # typographic apostrophes while preserving the visible credit text.
    $value = $value -replace '[\u2018\u2019]', "'"
    $value = $value -replace '(?m)[ \t]+$', ''
    return $value.Trim()
}
function Assert-Page([object]$Item, [int]$Expected, [string]$Name) {
    $actual = Get-ItemPage $Item
    if ($actual -ne $Expected) { Fail "$Name is on page $actual, expected $Expected." }
}
function Assert-Structure($Document, $State, $Registry) {
    if ([int]$Document.Pages.Count -ne [int]$State.expected_pages) { Fail "Document has $($Document.Pages.Count) pages, expected $($State.expected_pages)." }
    $front = Get-OneItem $Document 'LOOKBOOK_FRONT'
    $back = Get-OneItem $Document 'LOOKBOOK_BACK'
    Assert-Page $front 1 'LOOKBOOK_FRONT'
    Assert-Page $back ([int]$State.expected_pages) 'LOOKBOOK_BACK'
    if ($Registry.Count -ne [int]$State.look_count) { Fail "Registry count does not match controller look count." }
    for ($i = 0; $i -lt $Registry.Count; $i++) {
        $row = $Registry[$i]
        $expectedId = 'LOOK_{0:D3}' -f ($i + 1)
        if ($row.look_id -ne $expectedId) { Fail "Registry row $($i + 2): expected $expectedId." }
        $left = [int]$row.indd_left_page; $right = [int]$row.indd_right_page
        if ($left -ne 2 + 2 * $i -or $right -ne $left + 1) { Fail "$($row.look_id): registry pages are not the expected consecutive spread." }
    }
}
function Get-LockedFrameGeometry($Document) {
    $profile = @()
    # InDesign can persist either page- or spread-relative ruler origins between
    # COM launches. Store horizontal geometry relative to its parent page so the
    # structural lock is stable across those equivalent coordinate systems.
    $pageWidth = [double]$Document.DocumentPreferences.PageWidth
    foreach ($item in @(Get-DocumentPageItems $Document)) {
        $label = [string]$item.Label
        if ($label -notmatch '^LOOKBOOK_(LEFT_IMAGE|RIGHT_IMAGE|CREDITS)(\|LOOK_\d{3})?$') { continue }
        try {
            $bounds = @($item.GeometricBounds)
            if ($bounds.Count -ne 4) { Fail "$label has an invalid geometric-bounds value." }
            $left = [double]$bounds[1]
            $relativeLeft = $left % $pageWidth
            if ($relativeLeft -lt 0) { $relativeLeft += $pageWidth }
            $width = [double]$bounds[3] - [double]$bounds[1]
            $profile += [ordered]@{
                id = [int]$item.Id; label = $label
                top = [double]$bounds[0]; left = $relativeLeft
                bottom = [double]$bounds[2]; right = $relativeLeft + $width
            }
        } catch {
            Fail "$label does not expose safe geometric bounds."
        }
    }
    return @($profile | Sort-Object id)
}
function Get-GeometryCoordinate($Item, [string]$Coordinate) {
    return [double]$(if ($Item -is [System.Collections.IDictionary]) { $Item[$Coordinate] } else { $Item.PSObject.Properties[$Coordinate].Value })
}
function Get-CaptionGeometryEvidence([string]$CaptionGeometryPath) {
    $items = @{}
    if ([string]::IsNullOrWhiteSpace($CaptionGeometryPath)) { return $items }
    if (-not (Test-Path -LiteralPath $CaptionGeometryPath -PathType Leaf)) { Fail "Missing controlled caption geometry evidence: $CaptionGeometryPath" }
    $evidence = Read-Json $CaptionGeometryPath
    if ($evidence.schema -ne 1 -or $evidence.gate -ne 'captions' -or $null -eq $evidence.frames) { Fail 'Caption geometry evidence is malformed.' }
    foreach ($frame in @($evidence.frames)) {
        $id = [string]$frame.id
        if (-not $id -or $items.ContainsKey($id)) { Fail 'Caption geometry evidence contains an invalid or duplicate frame.' }
        $items[$id] = $frame
    }
    return $items
}
function Test-GeometryBounds($Actual, $Expected) {
    $actualValues = @($Actual); $expectedValues = @($Expected)
    if ($actualValues.Count -ne 4 -or $expectedValues.Count -ne 4) { return $false }
    for ($index = 0; $index -lt 4; $index++) {
        try { if ([math]::Abs(([double]$actualValues[$index]) - ([double]$expectedValues[$index])) -gt $GEOMETRY_EPSILON) { return $false } } catch { return $false }
    }
    return $true
}
function Get-VisualCaptionCorrections([string]$CorrectionPlanPath, $State) {
    $items = @{}
    if ([string]::IsNullOrWhiteSpace($CorrectionPlanPath) -or -not (Test-Path -LiteralPath $CorrectionPlanPath -PathType Leaf)) { return $items }
    $plan = Read-Json $CorrectionPlanPath
    if ($plan.schema -ne 1 -or $plan.generator -ne 'lookbook_gate.py:plan-clearance-corrections' -or $plan.session_id -ne $State.session_id) { Fail 'Caption-clearance correction plan is malformed or belongs to another session.' }
    if ($null -eq $plan.caption_corrections) { return $items }
    foreach ($correction in @($plan.caption_corrections)) {
        $lookId = [string]$correction.look_id
        if ($lookId -notmatch '^LOOK_\d{3}$' -or $items.ContainsKey("LOOKBOOK_CREDITS|$lookId")) { Fail 'Caption-clearance correction plan has an invalid or duplicate credits frame.' }
        $mode = [string]$correction.mode; $alignment = [string]$correction.paragraph_alignment
        if ($mode -notin @('DOWN','RIGHT')) { Fail "$lookId has an invalid credits-frame correction mode." }
        if (($mode -eq 'DOWN' -and $alignment -ne 'STYLE') -or ($mode -eq 'RIGHT' -and $alignment -ne 'RIGHT_ALIGN')) { Fail "$lookId has an invalid credits-frame paragraph alignment." }
        $before = @($correction.from_frame_bounds); $after = @($correction.to_frame_bounds)
        # ConvertFrom-Json exposes an omitted/JSON-null prior as `$null`; wrapping
        # it with @() produces a one-item array containing null, not an empty
        # optional geometry. Keep the distinction explicit for legacy plans.
        $prior = if ($null -eq $correction.prior_frame_bounds) { @() } else { @($correction.prior_frame_bounds) }
        if ($before.Count -ne 4 -or $after.Count -ne 4) { Fail "$lookId credits-frame correction has invalid bounds." }
        for ($index = 0; $index -lt 4; $index++) { try { [void][double]$before[$index]; [void][double]$after[$index] } catch { Fail "$lookId credits-frame correction has nonnumeric bounds." } }
        if ([double]$after[2] -le [double]$after[0] -or [double]$after[3] -le [double]$after[1]) { Fail "$lookId credits-frame correction has non-positive target bounds." }
        $deltaX = [double]$after[1] - [double]$before[1]; $deltaY = [double]$after[0] - [double]$before[0]
        if (($mode -eq 'DOWN' -and ([math]::Abs($deltaX) -gt $GEOMETRY_EPSILON -or $deltaY -le $GEOMETRY_EPSILON)) -or ($mode -eq 'RIGHT' -and ($deltaX -le $GEOMETRY_EPSILON -or $deltaY -lt -$GEOMETRY_EPSILON))) { Fail "$lookId credits-frame correction violates its permitted direction." }
        if ($prior.Count -ne 0 -and $prior.Count -ne 4) { Fail "$lookId credits-frame calibration has invalid prior bounds." }
        $items["LOOKBOOK_CREDITS|$lookId"] = [ordered]@{ mode = $mode; paragraph_alignment = $alignment; before = @($before); after = @($after); prior = @($prior) }
    }
    return $items
}
function Assert-Baseline($Document, [string]$StructureEvidencePath, [string]$CaptionGeometryPath = '', [bool]$AllowCreditExpansion = $false, [string]$VisualCaptionCorrectionPath = '', $State = $null) {
    $baseline = Read-Json $StructureEvidencePath
    if ([int]$baseline.page_item_count -ne (Get-DocumentPageItemCount $Document)) { Fail 'Page-item count changed after structure proof.' }
    if ([int]$baseline.text_frame_count -ne [int]$Document.TextFrames.Count) { Fail 'Text-frame count changed after structure proof.' }
    # Older sessions do not have geometry evidence. Keep them releasable, but
    # every session started with this controller locks the three look frames on
    # every spread so a visual correction can move only the graphic content.
    if ($null -eq $baseline.frame_geometry) { return }
    $expected = @($baseline.frame_geometry)
    $actual = @(Get-LockedFrameGeometry $Document)
    if ($expected.Count -eq 0 -or $expected.Count -ne $actual.Count) { Fail 'Locked image/credit frame count changed after structure proof.' }
    $actualById = @{}
    foreach ($item in $actual) { $actualById[[string]$item.id] = $item }
    $captionGeometry = Get-CaptionGeometryEvidence $CaptionGeometryPath
    $visualCaptionCorrections = if ($null -ne $State) { Get-VisualCaptionCorrections $VisualCaptionCorrectionPath $State } else { @{} }
    foreach ($item in $expected) {
        $id = [string]$item.id
        if (-not $actualById.ContainsKey($id)) { Fail "Locked frame $id was deleted or replaced after structure proof." }
        $now = $actualById[$id]
        $changed = $false
        foreach ($coordinate in @('top','left','bottom','right')) {
            # Geometry rows are ordered dictionaries when produced by the native
            # profiler.  PSObject.Properties does not expose their keys, which
            # silently casts to zero and produces a false frame-moved failure.
            $before = Get-GeometryCoordinate $item $coordinate
            $after = Get-GeometryCoordinate $now $coordinate
            if ([math]::Abs($after - $before) -gt $GEOMETRY_EPSILON) { $changed = $true }
        }
        if (-not $changed) { continue }
        # Credits are the one controlled exception: after preserving CREDiTs,
        # the existing frame may grow only downwards. It may never move, narrow,
        # widen, be replaced, or be threaded. Every later stage must match the
        # exact geometry evidence written by the captions transaction.
        if ([string]$now.label -notlike 'LOOKBOOK_CREDITS|*') { Fail "Locked frame $id moved or resized after structure proof." }
        $label = [string]$now.label
        if ($visualCaptionCorrections.ContainsKey($label)) {
            if (-not $captionGeometry.ContainsKey($id)) { Fail "$label has a visual correction but no saved captions geometry." }
            $baseBounds = @((Get-GeometryCoordinate $captionGeometry[$id] 'top'), (Get-GeometryCoordinate $captionGeometry[$id] 'left'), (Get-GeometryCoordinate $captionGeometry[$id] 'bottom'), (Get-GeometryCoordinate $captionGeometry[$id] 'right'))
            $nowBounds = @((Get-GeometryCoordinate $now 'top'), (Get-GeometryCoordinate $now 'left'), (Get-GeometryCoordinate $now 'bottom'), (Get-GeometryCoordinate $now 'right'))
            if ((Test-GeometryBounds -Actual $nowBounds -Expected $baseBounds) -or (Test-GeometryBounds -Actual $nowBounds -Expected @($visualCaptionCorrections[$label].after)) -or (@($visualCaptionCorrections[$label].prior).Count -eq 4 -and (Test-GeometryBounds -Actual $nowBounds -Expected @($visualCaptionCorrections[$label].prior)))) { continue }
            Fail "$label differs from both the saved captions geometry and its signed visual correction target."
        }
        foreach ($coordinate in @('top','left','right')) {
            $creditBefore = Get-GeometryCoordinate $item $coordinate
            $creditAfter = Get-GeometryCoordinate $now $coordinate
            if ([math]::Abs($creditBefore - $creditAfter) -gt $GEOMETRY_EPSILON) { Fail "Credits frame $id moved or changed width after structure proof (${coordinate}: before=$creditBefore after=$creditAfter, allowExpansion=$AllowCreditExpansion)." }
        }
        if ((Get-GeometryCoordinate $now 'bottom') + $GEOMETRY_EPSILON -lt (Get-GeometryCoordinate $item 'bottom')) { Fail "Credits frame $id was reduced instead of only growing downward." }
        if ($AllowCreditExpansion) { continue }
        if (-not $captionGeometry.ContainsKey($id)) { Fail "Credits frame $id changed without controlled caption geometry evidence (allowExpansion=$AllowCreditExpansion, geometryPath=$CaptionGeometryPath)." }
        foreach ($coordinate in @('top','left','bottom','right')) {
            if ([math]::Abs((Get-GeometryCoordinate $now $coordinate) - (Get-GeometryCoordinate $captionGeometry[$id] $coordinate)) -gt $GEOMETRY_EPSILON) { Fail "Credits frame $id differs from controlled caption geometry evidence." }
        }
    }
}
function Assert-Frames($Document, $Registry) {
    # Build a label index in one COM traversal.  Repeated Get-OneItem scans
    # exhaust InDesign's COM bridge on a 100+ page lookbook.
    $byLabel = @{}
    foreach ($item in @(Get-DocumentPageItems $Document)) {
        $label = [string]$item.Label
        if ($label) {
            if (-not $byLabel.ContainsKey($label)) { $byLabel[$label] = @() }
            $byLabel[$label] += $item
        }
    }
    foreach ($generic in @('LOOKBOOK_LEFT_IMAGE', 'LOOKBOOK_RIGHT_IMAGE', 'LOOKBOOK_CREDITS')) {
        if ($byLabel.ContainsKey($generic)) { Fail "Generic label remains: $generic" }
    }
    foreach ($row in $Registry) {
        $id = [string]$row.look_id
        $leftLabel = "LOOKBOOK_LEFT_IMAGE|$id"; $rightLabel = "LOOKBOOK_RIGHT_IMAGE|$id"; $creditsLabel = "LOOKBOOK_CREDITS|$id"
        if (-not $byLabel.ContainsKey($leftLabel) -or $byLabel[$leftLabel].Count -ne 1) { Fail "${leftLabel}: expected exactly one item." }
        if (-not $byLabel.ContainsKey($rightLabel) -or $byLabel[$rightLabel].Count -ne 1) { Fail "${rightLabel}: expected exactly one item." }
        if (-not $byLabel.ContainsKey($creditsLabel) -or $byLabel[$creditsLabel].Count -ne 1) { Fail "${creditsLabel}: expected exactly one item." }
        $left = $byLabel[$leftLabel][0]; $right = $byLabel[$rightLabel][0]; $credits = $byLabel[$creditsLabel][0]
        Assert-Page $left ([int]$row.indd_left_page) "$id left frame"
        Assert-Page $right ([int]$row.indd_right_page) "$id right frame"
        Assert-Page $credits ([int]$row.indd_left_page) "$id credits frame"
        try {
            if ([int]$credits.ParentStory.TextContainers.Count -ne 1) { Fail "$id credits frame is threaded." }
        } catch { Fail "$id credits frame is not a safe standalone text frame." }
    }
}
function New-LabelIndex($Document) {
    $byLabel = @{}
    foreach ($item in @(Get-DocumentPageItems $Document)) { if ([string]$item.Label) { $byLabel[[string]$item.Label] = $item } }
    return $byLabel
}
function Test-FrameImage($Frame, [string]$ExpectedFilename, [string]$ExpectedPath = '') {
    # A matching basename alone is unsafe: a new project can inherit the same
    # filename from an earlier lookbook while its link still points to that old
    # (and potentially missing) work area. Require one graphic, a normal link
    # status, and—when known—the frozen absolute source path.
    if ([int]$Frame.AllGraphics.Count -ne 1) { return $false }
    try {
        $link = $Frame.AllGraphics.Item(1).ItemLink
        if ([string]$link.Name -ne [System.IO.Path]::GetFileName($ExpectedFilename)) { return $false }
        if ([int]$link.Status -ne $LINK_NORMAL) { return $false }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedPath)) {
            if (-not (Test-Path -LiteralPath $ExpectedPath -PathType Leaf)) { return $false }
            if ((Normalize-Path ([string]$link.FilePath)) -ne (Normalize-Path $ExpectedPath)) { return $false }
        }
        return $true
    } catch { return $false }
}
function Assert-ImageRows($Document, $Rows, $ByLabel, [hashtable]$CompositionSources = $null, [string]$HiresPath = '') {
    if ($null -eq $ByLabel) { $ByLabel = New-LabelIndex $Document }
    foreach ($row in $Rows) {
        $id = [string]$row.look_id
        $leftFilename = [string]$row.left_filename; $rightFilename = [string]$row.right_filename
        if ($null -ne $CompositionSources -and $CompositionSources.ContainsKey($id)) {
            $leftFilename = [string]$CompositionSources[$id].left_image_filename
            $rightFilename = [string]$CompositionSources[$id].right_image_filename
        }
        foreach ($side in @(@('LEFT', $leftFilename), @('RIGHT', $rightFilename))) {
            $frameLabel = "LOOKBOOK_$($side[0])_IMAGE|$id"
            if (-not $byLabel.ContainsKey($frameLabel)) { Fail "${frameLabel}: expected exactly one item." }
            $frame = $byLabel[$frameLabel]
            if ([int]$frame.AllGraphics.Count -ne 1) { Fail "$id $($side[0]) frame has $($frame.AllGraphics.Count) graphics, expected one." }
            $expectedPath = if ([string]::IsNullOrWhiteSpace($HiresPath)) { '' } else { Join-Path $HiresPath ([string]$side[1]) }
            if (-not (Test-FrameImage $frame ([string]$side[1]) $expectedPath)) { Fail "$id $($side[0]) link is missing, out of date, or does not point to this project's frozen hires source." }
        }
    }
}
function Assert-Images($Document, $Registry, [hashtable]$CompositionSources = $null, [string]$HiresPath = '') { Assert-ImageRows $Document $Registry (New-LabelIndex $Document) $CompositionSources $HiresPath }
function Get-CompositionPlan($Control, $Registry) {
    $planPath = Join-Path $Control 'visual\composition-plan.tsv'
    $plan = @(Read-Tsv $planPath @('look_id','orientation','left_image_filename','right_image_filename','photo_adjustment_points','plan_status'))
    if ($plan.Count -ne $Registry.Count) { Fail "Composition plan has $($plan.Count) looks, expected $($Registry.Count)." }
    $validated = @()
    for ($index = 0; $index -lt $Registry.Count; $index++) {
        $row = $plan[$index]; $expected = [string]$Registry[$index].look_id
        if ([string]$row.look_id -ne $expected) { Fail "Composition plan row $($index + 2) must be $expected." }
        if ([string]$row.orientation -ne 'FULL_LEFT_CLOSE_RIGHT') { Fail "$expected does not declare full-length left and close-up right." }
        if ([string]$row.plan_status -ne 'READY') { Fail "$expected composition plan is not READY." }
        $registeredLeft = [string]$Registry[$index].left_filename; $registeredRight = [string]$Registry[$index].right_filename
        $actualLeft = [string]$row.left_image_filename; $actualRight = [string]$row.right_image_filename
        $registeredOrder = $actualLeft -eq $registeredLeft -and $actualRight -eq $registeredRight
        $reversedOrder = $actualLeft -eq $registeredRight -and $actualRight -eq $registeredLeft
        if (-not ($registeredOrder -or $reversedOrder)) { Fail "$expected composition-plan filenames do not match its two registry images." }
        $shift = 0.0
        if (-not [double]::TryParse([string]$row.photo_adjustment_points, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$shift) -or [double]::IsNaN($shift) -or [double]::IsInfinity($shift) -or [math]::Abs($shift) -gt $MAX_COMPOSITION_SHIFT_POINTS) { Fail "$expected has an invalid or excessive horizontal photo adjustment." }
        $validated += $row
    }
    return @($validated)
}
function Get-CompositionImageSources($Control, $Registry) {
    $plan = @(Get-CompositionPlan $Control $Registry)
    $sources = @{}
    for ($index = 0; $index -lt $Registry.Count; $index++) {
        $row = $plan[$index]; $expected = [string]$Registry[$index].look_id
        $actualLeft = [string]$row.left_image_filename; $actualRight = [string]$row.right_image_filename
        $sources[$expected] = @{ left_image_filename = $actualLeft; right_image_filename = $actualRight }
    }
    return $sources
}
function Get-CreditsParagraphStyle($Document) {
    try { $style = $Document.ParagraphStyles.Item('CREDiTs') } catch { Fail 'Required CREDiTs paragraph style is missing.' }
    if ([string]$style.Name -ne 'CREDiTs') { Fail 'Required CREDiTs paragraph style is missing.' }
    return $style
}
function Get-DocumentFindTextPreferences($Document) {
    # InDesign occasionally disconnects Document.Parent after a long native
    # transaction even though the document itself is still valid. Resolve the
    # current Find/Change preferences through the document first, then use the
    # application fallback; verify a readable property before returning it.
    foreach ($candidate in @(
        { $Document.FindTextPreferences },
        { $Document.Parent.FindTextPreferences }
    )) {
        try {
            $preferences = & $candidate
            if ($null -eq $preferences) { continue }
            $null = $preferences.FindWhat
            return $preferences
        } catch {}
    }
    Fail 'Current document does not expose usable Find/Change text preferences.'
}
function Get-CaptionGroups($Captions) {
    $grouped = @{}
    foreach ($row in $Captions) {
        if (-not $grouped.ContainsKey($row.look_id)) { $grouped[$row.look_id] = @() }
        $grouped[$row.look_id] += $row
    }
    return $grouped
}
function Get-CreditsNestedStyle($Document, [int]$FieldIndex, [string]$LookId) {
    $name = @('CATEGORY','BRAND','PRICE','SKU')[$FieldIndex % 4]
    try { $style = $Document.CharacterStyles.Item($name) } catch { Fail "$LookId required nested credits style $name is missing." }
    if ([string]$style.Name -ne $name) { Fail "$LookId required nested credits style $name is missing." }
    return $style
}
function Assert-CaptionTypography($Paragraph, $ParagraphStyle, $CharacterStyle, [string]$LookId, [int]$FieldIndex, $ExpectedJustification = $null) {
    # CREDiTs intentionally has nested styles: CATEGORY / BRAND / PRICE / SKU.
    # The visible point size and leading therefore differ by the field within a
    # product. Verify the paragraph rhythm against CREDiTs and the glyph rhythm
    # against the appropriate nested character style instead of flattening the
    # four fields to one misleading leading value.
    foreach ($property in @('SpaceBefore','SpaceAfter','Justification')) {
        try {
            $actual = [double]$Paragraph.$property
            $expected = if ($property -eq 'Justification' -and $null -ne $ExpectedJustification) { [double]$ExpectedJustification } else { [double]$ParagraphStyle.$property }
        } catch { Fail "$LookId credits cannot verify CREDiTs paragraph $property." }
        if ([double]::IsNaN($actual) -or [double]::IsInfinity($actual) -or [math]::Abs($actual - $expected) -gt 0.01) {
            Fail "$LookId credits paragraph $property does not equal CREDiTs."
        }
    }
    $rangeCount = [int]$Paragraph.TextStyleRanges.Count
    if ($rangeCount -eq 0) { Fail "$LookId credits field $($FieldIndex + 1) has no visible text range." }
    for ($rangeIndex = 1; $rangeIndex -le $rangeCount; $rangeIndex++) {
        $range = $Paragraph.TextStyleRanges.Item($rangeIndex)
        if ([string]$range.AppliedCharacterStyle.Name -ne [string]$CharacterStyle.Name) { Fail "$LookId credits field $($FieldIndex + 1) does not use nested style $($CharacterStyle.Name)." }
        foreach ($property in @('PointSize','Leading','Tracking')) {
            try {
                $expectedRaw = $CharacterStyle.$property
                # A character style can deliberately leave tracking inherited.
                # InDesign then exposes an effective numeric value on the text
                # range, while the style exposes NothingEnum. That is correct
                # and must not be mistaken for a formatting override.
                if ($null -eq $expectedRaw -or [int]$expectedRaw -eq $NOTHING) { continue }
                $actual = [double]$range.$property
                $expected = [double]$expectedRaw
            } catch { Fail "$LookId credits cannot verify nested style $($CharacterStyle.Name) $property." }
            if ([double]::IsNaN($actual) -or [double]::IsInfinity($actual) -or [math]::Abs($actual - $expected) -gt 0.01) {
                Fail "$LookId credits field $($FieldIndex + 1) $property does not equal nested style $($CharacterStyle.Name)."
            }
        }
    }
}
function Apply-CreditsParagraphStyle($Paragraph, $Style, $CharacterStyle, [string]$LookId) {
    # Apply/clear first to remove unknown paragraph overrides. Reapply the
    # paragraph style afterwards. The source template's GREP styles are not a
    # safe substitute for this step: a numeric price can also match the brand
    # expression. Apply the exact existing field style explicitly.
    $Paragraph.ApplyParagraphStyle($Style, $true)
    $Paragraph.ClearOverrides($OVERRIDE_ALL)
    $Paragraph.ApplyParagraphStyle($Style, $true)
    $Paragraph.ApplyCharacterStyle($CharacterStyle)
}
function Assert-CreditsStartAtTop($Frame, [string]$LookId) {
    # A short credit block must not be vertically centred in a tall existing
    # container. Its first field begins at the frame's normal top inset; any
    # whitespace above it must come only from that explicit inset, never from
    # vertical justification or an empty leading paragraph.
    try {
        if ([int]$Frame.TextFramePreferences.VerticalJustification -ne $TOP_ALIGN) { Fail "$LookId credits are not top-aligned in their existing frame." }
    } catch { Fail "$LookId credits cannot verify vertical frame alignment." }
    $raw = Get-Text $Frame
    if ($raw -match '^[\r\n\t]') { Fail "$LookId credits begin with an empty paragraph or tab." }
}
function Assert-CaptionBlock($Document, $Row, $Grouped, $Style, $ByLabel, [bool]$RightAligned = $false) {
    $id = [string]$Row.look_id
    if (-not $Grouped.ContainsKey($id)) { Fail "$id has no caption data." }
    $expectedLines = @()
    foreach ($caption in $Grouped[$id]) { $expectedLines += @($caption.type, $caption.brand, $caption.price, $caption.article) }
    if ($null -ne $ByLabel) {
        $label = "LOOKBOOK_CREDITS|$id"
        if (-not $ByLabel.ContainsKey($label)) { Fail "${label}: expected exactly one item." }
        $frame = $ByLabel[$label]
    } else {
        $frame = Get-OneItem $Document "LOOKBOOK_CREDITS|$id"
    }
    Assert-CreditsStartAtTop $frame $id
    $raw = Get-Text $frame
    if ($raw.IndexOf("`t", [System.StringComparison]::Ordinal) -ge 0) { Fail "$id credits contain tabs; Find/Change normalization was not completed." }
    # A frame with overset text can still expose the complete ParentStory, so
    # comparing story contents alone falsely passed a clipped final product.
    # The fixed credits container must visibly contain every required line.
    if ([bool]$frame.Overflows) { Fail "$id credits overflow the fixed credits frame." }
    $actual = Normalize-Text $raw
    $expected = Normalize-Text ($expectedLines -join "`n")
    if ($actual -ne $expected) { Fail "$id caption text differs from caption-data.tsv." }
    $paragraphCount = [int]$frame.ParentStory.Paragraphs.Count
    if ($paragraphCount -ne $expectedLines.Count) { Fail "$id credits have $paragraphCount fields, expected $($expectedLines.Count)." }
    for ($index = 0; $index -lt $paragraphCount; $index++) {
        $paragraph = $frame.ParentStory.Paragraphs.Item($index + 1)
        if ([string]$paragraph.AppliedParagraphStyle.Name -ne [string]$Style.Name) { Fail "$id credits do not use the CREDiTs paragraph style." }
        $expectedJustification = if ($RightAligned) { $RIGHT_ALIGN } else { $null }
        Assert-CaptionTypography $paragraph $Style (Get-CreditsNestedStyle $Document $index $id) $id $index $expectedJustification
    }
    return $actual
}
function Assert-Captions($Document, $Registry, $Captions, $VisualCaptionCorrections = @{}) {
    $grouped = Get-CaptionGroups $Captions
    $style = Get-CreditsParagraphStyle $Document
    $unique = @{}
    foreach ($row in $Registry) {
        $label = "LOOKBOOK_CREDITS|$([string]$row.look_id)"
        $rightAligned = $VisualCaptionCorrections.ContainsKey($label) -and [string]$VisualCaptionCorrections[$label].mode -eq 'RIGHT'
        $actual = Assert-CaptionBlock $Document $row $grouped $style $null $rightAligned
        $unique[$actual] = $true
    }
    if ($Registry.Count -gt 1 -and $unique.Keys.Count -lt 2) { Fail 'All caption blocks are identical.' }
}
function Fit-CreditsToFixedFrame($Frame, [string]$LookId) {
    # CREDiTs is the typography authority. Never shrink its point size, leading
    # or product spacing locally. Normalise the *existing* text container to
    # top alignment, then, only when the correct block is overset, grow that
    # same container downwards. No frame is added, recreated, moved, widened,
    # narrowed, or threaded.
    $preferences = $Frame.TextFramePreferences
    $preferences.AutoSizingType = 1330005536 # AutoSizingTypeEnum.OFF
    $preferences.VerticalJustification = $TOP_ALIGN
    Assert-CreditsStartAtTop $Frame $LookId
    $before = @($Frame.GeometricBounds)
    if (-not [bool]$Frame.Overflows) { return [ordered]@{ before = $before; after = @($Frame.GeometricBounds); expanded = $false } }
    if ($null -eq $Frame.ParentPage) { Fail "$LookId credits frame has no parent page for downward expansion." }
    $rawPageBounds = $Frame.ParentPage.Bounds
    $pageBounds = @()
    foreach ($value in $rawPageBounds) { $pageBounds += [double]$value }
    if ($pageBounds.Count -ne 4) { Fail "$LookId credits page has invalid bounds." }
    $top = [double]$before[0]; $left = [double]$before[1]; $bottom = [double]$before[2]; $right = [double]$before[3]
    $limit = [double]$pageBounds[2]
    if ($limit -le $bottom + 0.01) { Fail "$LookId credits overflow and cannot grow down inside the page." }
    $iterations = 0
    while ([bool]$Frame.Overflows -and $bottom -lt $limit - 0.01) {
        $iterations++
        if ($iterations -gt 200) { Fail "$LookId credits downward expansion exceeded its safety limit." }
        $bottom = [math]::Min($limit, $bottom + 18.0)
        $Frame.GeometricBounds = @($top, $left, $bottom, $right)
        $now = @($Frame.GeometricBounds)
        if ([math]::Abs(([double]$now[0]) - $top) -gt 0.01 -or [math]::Abs(([double]$now[1]) - $left) -gt 0.01 -or [math]::Abs(([double]$now[3]) - $right) -gt 0.01) { Fail "$LookId credits expansion moved or changed the width of its existing frame." }
    }
    if ([bool]$Frame.Overflows) { Fail "$LookId credits still overflow after growing the existing frame downward to the page bottom." }
    Assert-CreditsStartAtTop $Frame $LookId
    return [ordered]@{ before = $before; after = @($Frame.GeometricBounds); expanded = $true }
}
function Set-CaptionFrameBounds($Frame, $Bounds, [string]$LookId) {
    if ($null -eq $Frame) { Fail "$LookId planned credits frame is null." }
    $raw = @($Bounds)
    $expected = @()
    foreach ($value in $raw) { try { $expected += [double]$value } catch { Fail "$LookId has a nonnumeric planned credits-frame coordinate." } }
    if ($expected.Count -ne 4) { Fail "$LookId has invalid planned credits-frame bounds." }
    try {
        # COM requires a flat numeric SAFEARRAY. A nested PowerShell object[]
        # can otherwise become a null VARIANT even though its displayed values
        # look correct.
        $Frame.GeometricBounds = @([double]$expected[0], [double]$expected[1], [double]$expected[2], [double]$expected[3])
    } catch {
        try { $frameId = [string]$Frame.Id } catch { $frameId = 'unknown' }
        Fail "$LookId cannot write planned existing credits-frame bounds (frame=$frameId; bounds=$($expected -join ','))."
    }
    $actual = @($Frame.GeometricBounds)
    if (-not (Test-GeometryBounds -Actual $actual -Expected $expected)) { Fail "$LookId did not retain the planned geometry of its existing credits frame." }
    if ($null -eq $Frame.ParentPage) { Fail "$LookId credits frame lost its parent page." }
}
function Stabilize-CreditsFrameBeforeRewrite($Frame, [string]$LookId) {
    # The supplied template has width-and-height auto-sizing enabled on its
    # credits frames. Replacing story contents while it is active narrows the
    # frame before the later fitting routine can turn it off. Freeze the
    # existing frame first, then explicitly restore its original geometry.
    # This touches only the existing labelled frame; it never adds a frame or
    # changes the intended credits column width.
    $baseBounds = @($Frame.GeometricBounds)
    if ($baseBounds.Count -ne 4) { Fail "$LookId credits frame has invalid geometry before rewrite." }
    $preferences = $Frame.TextFramePreferences
    $preferences.AutoSizingType = 1330005536 # AutoSizingTypeEnum.OFF
    $preferences.VerticalJustification = $TOP_ALIGN
    Set-CaptionFrameBounds -Frame $Frame -Bounds $baseBounds -LookId $LookId
    $now = @($Frame.GeometricBounds)
    if (-not (Test-GeometryBounds -Actual $now -Expected $baseBounds)) {
        Fail "$LookId credits frame changed geometry while disabling auto-sizing."
    }
    Assert-CreditsStartAtTop $Frame $LookId
}
function Restore-CaptionFrameBase($Frame, $BaseGeometry, $Style, [string]$LookId) {
    if ($null -eq $BaseGeometry) { Fail "$LookId is missing saved captions geometry." }
    Set-CaptionFrameBounds -Frame $Frame -Bounds (@((Get-GeometryCoordinate $BaseGeometry 'top'), (Get-GeometryCoordinate $BaseGeometry 'left'), (Get-GeometryCoordinate $BaseGeometry 'bottom'), (Get-GeometryCoordinate $BaseGeometry 'right'))) -LookId $LookId
    $Frame.TextFramePreferences.VerticalJustification = $TOP_ALIGN
    foreach ($paragraph in $Frame.ParentStory.Paragraphs) { $paragraph.Justification = $Style.Justification }
    Assert-CreditsStartAtTop $Frame $LookId
    if ([bool]$Frame.Overflows) { Fail "$LookId credits overflow after restoring the saved frame geometry." }
}
function Apply-VisualCaptionCorrection($Frame, $Correction, $Style, [string]$LookId) {
    if ($null -eq $Correction) { return $null }
    Set-CaptionFrameBounds -Frame $Frame -Bounds (@($Correction.after)) -LookId $LookId
    $Frame.TextFramePreferences.VerticalJustification = $TOP_ALIGN
    if ([string]$Correction.mode -eq 'RIGHT') {
        foreach ($paragraph in $Frame.ParentStory.Paragraphs) { $paragraph.Justification = $RIGHT_ALIGN }
    } else {
        foreach ($paragraph in $Frame.ParentStory.Paragraphs) { $paragraph.Justification = $Style.Justification }
    }
    Assert-CreditsStartAtTop $Frame $LookId
    if ([bool]$Frame.Overflows) { Fail "$LookId credits overflow after the planned existing-frame clearance correction." }
    return [ordered]@{ mode = [string]$Correction.mode; paragraph_alignment = [string]$Correction.paragraph_alignment; after_frame_bounds = @($Frame.GeometricBounds) }
}
function Assert-CaptionCorrectionInsideSafeArea($Frame, [string]$LookId) {
    if ($null -eq $Frame.ParentPage) { Fail "$LookId corrected credits frame has no parent page." }
    $pageBounds = @($Frame.ParentPage.Bounds)
    $frameBounds = @(Get-PageItemBounds $Frame "$LookId corrected credits frame")
    if ($pageBounds.Count -ne 4) { Fail "$LookId corrected credits page has invalid bounds." }
    $margins = $Frame.ParentPage.MarginPreferences
    $pageTop = [double]($pageBounds[0]); $pageLeft = [double]($pageBounds[1])
    $pageBottom = [double]($pageBounds[2]); $pageRight = [double]($pageBounds[3])
    $marginTop = [double]($margins.Top); $marginLeft = [double]($margins.Left)
    $marginBottom = [double]($margins.Bottom); $marginRight = [double]($margins.Right)
    $safe = @(
        ($pageTop + $marginTop + $SAFE_AREA_INTERIOR)
        ($pageLeft + $marginLeft + $SAFE_AREA_INTERIOR)
        ($pageBottom - $marginBottom - $SAFE_AREA_INTERIOR)
        $pageRight - $marginRight - $SAFE_AREA_INTERIOR
    )
    if ($safe[2] -le $safe[0] -or $safe[3] -le $safe[1]) { Fail "$LookId page safe area is too small for a credits correction." }
    if ($frameBounds[0] -lt $safe[0] - $GEOMETRY_EPSILON -or $frameBounds[1] -lt $safe[1] - $GEOMETRY_EPSILON -or $frameBounds[2] -gt $safe[2] + $GEOMETRY_EPSILON -or $frameBounds[3] -gt $safe[3] + $GEOMETRY_EPSILON) {
        Fail "$LookId corrected credits frame is outside the required interior safe area."
    }
    return $safe
}
function Assert-VisualCaptionCorrectionsApplied($Document, $VisualCaptionCorrections) {
    if ($VisualCaptionCorrections.Count -eq 0) { return }
    $byLabel = New-LabelIndex $Document
    foreach ($label in $VisualCaptionCorrections.Keys) {
        if (-not $byLabel.ContainsKey($label)) { Fail "$label is missing for planned credits-frame correction." }
        $frame = $byLabel[$label]
        $correction = $VisualCaptionCorrections[$label]
        if (-not (Test-GeometryBounds -Actual @($frame.GeometricBounds) -Expected @($correction.after))) { Fail "$label does not match its planned existing-frame correction geometry." }
        Assert-CreditsStartAtTop $frame (([string]$label).Split('|')[1])
        if ([bool]$frame.Overflows) { Fail "$label overflows after its planned visual correction." }
        if ([string]$correction.mode -eq 'RIGHT') {
            foreach ($paragraph in $frame.ParentStory.Paragraphs) {
                if ([int]$paragraph.Justification -ne $RIGHT_ALIGN) { Fail "$label is not right-aligned after its right-side correction." }
            }
        }
    }
}
function Write-CaptionGeometryEvidence($Control, $State, $Arm, [string]$MasterPath, $Document) {
    $frames = @()
    foreach ($item in @(Get-DocumentPageItems $Document)) {
        $label = [string]$item.Label
        if ($label -notlike 'LOOKBOOK_CREDITS|*') { continue }
        $bounds = @($item.GeometricBounds)
        if ($bounds.Count -ne 4) { Fail "$label does not expose safe credits bounds." }
        $frames += [ordered]@{ id = [int]$item.Id; label = $label; top = [double]$bounds[0]; left = [double]$bounds[1]; bottom = [double]$bounds[2]; right = [double]$bounds[3] }
    }
    if ($frames.Count -ne [int]$State.look_count) { Fail "Caption geometry profile has $($frames.Count) frames, expected $($State.look_count)." }
    $payload = [ordered]@{
        schema = 1; generator = 'run_lookbook_gate_com.ps1:captions-geometry'; session_id = [string]$State.session_id; gate = 'captions'; nonce = [string]$Arm.nonce
        created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $MasterPath; frames = @($frames | Sort-Object id)
    }
    Write-Json (Join-Path $Control 'evidence\caption-geometry.json') $payload
}
function Assert-Dates($Document, $State) {
    $show = Get-OneItem $Document 'LOOKBOOK_SHOW_DATE'
    $legal = Get-OneItem $Document 'LOOKBOOK_LEGAL'
    Assert-Page $show 1 'LOOKBOOK_SHOW_DATE'
    Assert-Page $legal ([int]$State.expected_pages) 'LOOKBOOK_LEGAL'
    if ((Get-Text $show).IndexOf([string]$State.show_text, [System.StringComparison]::Ordinal) -lt 0) { Fail 'Front date text does not match state.show_text.' }
    if ((Get-Text $legal).IndexOf([string]$State.legal_date, [System.StringComparison]::Ordinal) -lt 0) { Fail 'Legal date does not match state.legal_date.' }
}
function Assert-Visual($Control, $State, [string]$MasterPath) {
    $proof = Read-Json (Join-Path $Control 'evidence\visual.json')
    $arm = Read-Json (Join-Path $Control 'arms\visual.json')
    if ($proof.session_id -ne $State.session_id -or $proof.gate -ne 'visual' -or -not $proof.passed -or $proof.nonce -ne $arm.nonce) { Fail 'Visual evidence does not match the current session.' }
    if (-not (Same-Identity $proof.master (Master-Identity $MasterPath))) { Fail 'Visual proof belongs to a different master checkpoint.' }
}
function Write-Evidence($Control, $State, $Arm, [string]$GateName, [string]$MasterPath, $Document) {
    $checks = if ($GateName -eq 'release') { @('structure', 'dates', 'frames', 'images', 'captions', 'visual') } else { @($GateName) }
    $evidence = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = $GateName; passed = $true
        nonce = [string]$Arm.nonce; created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        master = Master-Identity $MasterPath; page_item_count = (Get-DocumentPageItemCount $Document)
        text_frame_count = [int]$Document.TextFrames.Count; checks = $checks
    }
    if ($GateName -eq 'structure') {
        $geometry = @(Get-LockedFrameGeometry $Document)
        if ($geometry.Count -ne (3 * [int]$State.look_count)) { Fail "Structure geometry profile has $($geometry.Count) frames, expected $(3 * [int]$State.look_count)." }
        $evidence.frame_geometry = $geometry
        # InDesign assigns fresh internal object IDs when an INDD is copied to
        # the next correction revision.  Record the revision separately from
        # the saved-master identity, which naturally changes after later
        # native gates save dates, links and captions.
        $evidence.structure_revision = if ($null -ne $State.PSObject.Properties['structure_revision']) { [int]$State.structure_revision } else { [int]$(if ($null -ne $State.PSObject.Properties['current_revision']) { $State.current_revision } else { 1 }) }
    }
    Write-Json (Join-Path $Control "evidence\$GateName.json") $evidence
}
function Apply-Structure($Document, $State, $Registry) {
    if ([int]$Document.Pages.Count -ne 4 -or [int]$Document.Spreads.Count -ne 3) { Fail 'Structure automation requires the pristine four-page automation template.' }
    $front = Get-OneItem $Document 'LOOKBOOK_FRONT'; $back = Get-OneItem $Document 'LOOKBOOK_BACK'
    Assert-Page $front 1 'LOOKBOOK_FRONT'; Assert-Page $back 4 'LOOKBOOK_BACK'
    $left = Get-OneItem $Document 'LOOKBOOK_LEFT_IMAGE'; $right = Get-OneItem $Document 'LOOKBOOK_RIGHT_IMAGE'; $credits = Get-OneItem $Document 'LOOKBOOK_CREDITS'
    Assert-Page $left 2 'Template left frame'; Assert-Page $right 3 'Template right frame'; Assert-Page $credits 2 'Template credits frame'
    $baseSpread = $left.ParentPage.Parent; $backSpread = $back.ParentPage.Parent; $created = @()
    try {
        for ($number = 2; $number -le [int]$State.look_count; $number++) {
            $copy = $baseSpread.Duplicate($LOC_BEFORE, $backSpread)
            if ([int]$copy.Pages.Count -ne 2) { Fail 'A duplicated working spread does not contain exactly two pages.' }
            $created += $copy
        }
        Assert-Structure $Document $State $Registry
    } catch {
        for ($index = $created.Count - 1; $index -ge 0; $index--) { try { $created[$index].Delete() } catch {} }
        throw
    }
}
function Apply-Dates($Document, $State) {
    $show = Get-OneItem $Document 'LOOKBOOK_SHOW_DATE'; $legal = Get-OneItem $Document 'LOOKBOOK_LEGAL'
    $show.Contents = [string]$State.show_text
    $before = Get-Text $legal
    $matches = [regex]::Matches($before, '\d{2}\.\d{2}\.\d{4}')
    if ($matches.Count -ne 1) { Fail 'Legal text must contain exactly one calendar date.' }
    $start = $matches[0].Index; $finish = $start + $matches[0].Length - 1
    $replacement = [string]$State.legal_date
    # InDesign 2026 exposes a writable Characters collection through COM, but
    # rejects a range-level Contents assignment from PowerShell. Characters are
    # one-based, whereas .NET regex indexes are zero-based. Replacing the ten
    # existing characters individually preserves every other legal character.
    for ($index = 0; $index -lt $replacement.Length; $index++) {
        $legal.ParentStory.Characters.Item($start + 1 + $index).Contents = $replacement.Substring($index, 1)
    }
    $after = Get-Text $legal
    $expected = $before.Substring(0, $start) + [string]$State.legal_date + $before.Substring($start + $matches[0].Length)
    if ($after -ne $expected) { Fail 'Legal-date postcondition failed; no whole-text replacement is permitted.' }
}
function Apply-Frames($Document, $Registry) {
    # Query the document's page-item collection once per generic label.  Repeating
    # that full COM enumeration for every look can disconnect InDesign on large
    # documents before frame evidence is written.
    $leftItems = @(Get-LabeledItems $Document 'LOOKBOOK_LEFT_IMAGE')
    $rightItems = @(Get-LabeledItems $Document 'LOOKBOOK_RIGHT_IMAGE')
    $creditItems = @(Get-LabeledItems $Document 'LOOKBOOK_CREDITS')
    if ($leftItems.Count -eq 0 -and $rightItems.Count -eq 0 -and $creditItems.Count -eq 0) {
        # A COM interruption can occur after all labels have been applied but
        # before the controller records evidence.  Verify that exact completed
        # state rather than attempting to relabel an already-complete document.
        Assert-Frames $Document $Registry
        return
    }
    foreach ($row in $Registry) {
        $leftCandidates = @($leftItems | Where-Object { (Get-ItemPage $_) -eq [int]$row.indd_left_page })
        $rightCandidates = @($rightItems | Where-Object { (Get-ItemPage $_) -eq [int]$row.indd_right_page })
        $creditCandidates = @($creditItems | Where-Object { (Get-ItemPage $_) -eq [int]$row.indd_left_page })
        if ($leftCandidates.Count -ne 1 -or $rightCandidates.Count -ne 1 -or $creditCandidates.Count -ne 1) { Fail "$($row.look_id): generic template frames are missing or ambiguous." }
        $leftCandidates[0].Label = "LOOKBOOK_LEFT_IMAGE|$($row.look_id)"
        $rightCandidates[0].Label = "LOOKBOOK_RIGHT_IMAGE|$($row.look_id)"
        $creditCandidates[0].Label = "LOOKBOOK_CREDITS|$($row.look_id)"
    }
}
function Get-CompletedImageLookIds($Registry, $ByLabel, [string]$Hires) {
    $completed = @()
    foreach ($row in $Registry) {
        $id = [string]$row.look_id
        $leftLabel = "LOOKBOOK_LEFT_IMAGE|$id"; $rightLabel = "LOOKBOOK_RIGHT_IMAGE|$id"
        if (-not $ByLabel.ContainsKey($leftLabel) -or -not $ByLabel.ContainsKey($rightLabel)) { continue }
        $leftSource = Join-Path $Hires ([string]$row.left_filename); $rightSource = Join-Path $Hires ([string]$row.right_filename)
        if ((Test-FrameImage $ByLabel[$leftLabel] ([string]$row.left_filename) $leftSource) -and (Test-FrameImage $ByLabel[$rightLabel] ([string]$row.right_filename) $rightSource)) { $completed += $id }
    }
    return @($completed)
}
function Write-ImageProgress($Control, $State, $Arm, [string]$MasterPath, $Document, $Registry, [string]$Hires) {
    $complete = @(Get-CompletedImageLookIds $Registry (New-LabelIndex $Document) $Hires)
    $progress = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = 'images'; nonce = [string]$Arm.nonce
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        master = Master-Identity $MasterPath; completed_looks = $complete; completed_count = [int]$complete.Count; look_count = [int]$Registry.Count
    }
    Write-Json (Join-Path $Control 'progress\images.json') $progress
    return $progress
}
function Apply-ImagesBatch($Document, $Registry, [string]$Hires, [int]$MaximumLooks) {
    $byLabel = New-LabelIndex $Document
    $pending = @()
    foreach ($row in $Registry) {
        $id = [string]$row.look_id
        $leftLabel = "LOOKBOOK_LEFT_IMAGE|$id"; $rightLabel = "LOOKBOOK_RIGHT_IMAGE|$id"
        if (-not $byLabel.ContainsKey($leftLabel) -or -not $byLabel.ContainsKey($rightLabel)) { Fail "$id image frames are missing." }
        $leftSource = Join-Path $Hires ([string]$row.left_filename); $rightSource = Join-Path $Hires ([string]$row.right_filename)
        if (-not ((Test-FrameImage $byLabel[$leftLabel] ([string]$row.left_filename) $leftSource) -and (Test-FrameImage $byLabel[$rightLabel] ([string]$row.right_filename) $rightSource))) { $pending += $row }
    }
    $batch = @($pending | Select-Object -First $MaximumLooks)
    foreach ($row in $batch) {
        foreach ($side in @(@('LEFT', [string]$row.left_filename), @('RIGHT', [string]$row.right_filename))) {
            $source = Join-Path $Hires $side[1]
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Fail "$($row.look_id): missing hires file $($side[1])." }
            $frameLabel = "LOOKBOOK_$($side[0])_IMAGE|$($row.look_id)"
            if (-not $byLabel.ContainsKey($frameLabel)) { Fail "${frameLabel}: expected exactly one item." }
            $frame = $byLabel[$frameLabel]
            if (Test-FrameImage $frame ([string]$side[1]) $source) { continue }
            # Placing directly into an existing graphic frame replaces its one
            # graphic in InDesign. Deleting it first is both slower and risks a
            # blank frame if the COM worker is interrupted between calls.
            $frame.Place($source, $false) | Out-Null
            if ([int]$frame.AllGraphics.Count -ne 1) { Fail "$($row.look_id) $($side[0]) place operation did not leave exactly one graphic." }
            $frame.Fit($FIT_FILL_PROPORTIONALLY)
        }
    }
    Assert-ImageRows $Document $batch $byLabel $null $Hires
    return $batch
}
function Get-CompletedCaptionLookIds($Document, $Registry, $Captions, $ByLabel) {
    $complete = @(); $grouped = Get-CaptionGroups $Captions; $style = Get-CreditsParagraphStyle $Document
    foreach ($row in $Registry) {
        try { [void](Assert-CaptionBlock $Document $row $grouped $style $ByLabel); $complete += [string]$row.look_id } catch {}
    }
    return @($complete)
}
function Write-CaptionProgress($Control, $State, $Arm, [string]$MasterPath, $Document, $Registry, $Captions) {
    $complete = @(Get-CompletedCaptionLookIds $Document $Registry $Captions (New-LabelIndex $Document))
    $progress = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = 'captions'; nonce = [string]$Arm.nonce
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        master = Master-Identity $MasterPath; completed_looks = $complete; completed_count = [int]$complete.Count; look_count = [int]$Registry.Count
    }
    Write-Json (Join-Path $Control 'progress\captions.json') $progress
    return $progress
}
function Write-CaptionRepairProgress($Control, $State, $Arm, [string]$MasterPath, $Document, $Registry, $Captions) {
    $complete = @(Get-CompletedCaptionLookIds $Document $Registry $Captions (New-LabelIndex $Document))
    $progress = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = 'visual-caption-repair'; nonce = [string]$Arm.nonce
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        master = Master-Identity $MasterPath; completed_looks = $complete; completed_count = [int]$complete.Count; look_count = [int]$Registry.Count
    }
    Write-Json (Join-Path $Control 'progress\caption-repair.json') $progress
    return $progress
}
function Apply-CaptionsBatch($Document, $Registry, $Captions, [int]$MaximumLooks) {
    $grouped = Get-CaptionGroups $Captions
    $style = Get-CreditsParagraphStyle $Document
    $byLabel = New-LabelIndex $Document
    $completed = @(Get-CompletedCaptionLookIds $Document $Registry $Captions $byLabel)
    $pending = @($Registry | Where-Object { $completed -notcontains [string]$_.look_id })
    $batch = @($pending | Select-Object -First $MaximumLooks)
    try {
        $findPreferences = Get-DocumentFindTextPreferences $Document
        $findPreferences.FindWhat = ''
        foreach ($row in $batch) {
            $id = [string]$row.look_id
            if (-not $grouped.ContainsKey($id)) { Fail "$id has no caption data." }
            # The approved template uses one CREDiTs paragraph per field.  Write
            # tab-delimited source fields, then perform the required Find/Change
            # conversion from tabs to paragraph returns inside this exact frame.
            $fields = @(); foreach ($caption in $grouped[$id]) { $fields += @($caption.type, $caption.brand, $caption.price, $caption.article) }
            $frameLabel = "LOOKBOOK_CREDITS|$id"
            if (-not $byLabel.ContainsKey($frameLabel)) { Fail "${frameLabel}: expected exactly one item." }
            $frame = $byLabel[$frameLabel]
            if ([int]$frame.ParentStory.TextContainers.Count -ne 1) { Fail "$id credits frame is threaded." }
            # Disable the template's width-and-height auto-sizing before
            # changing the story. Doing it afterwards lets InDesign shrink
            # the existing credits column and violates its fixed-width rule.
            Stabilize-CreditsFrameBeforeRewrite $frame $id
            $story = $frame.ParentStory
            $story.Contents = ($fields -join "`t")
            $findPreferences.FindWhat = "`t"
            $matches = @($story.FindText())
            if ($matches.Count -ne ($fields.Count - 1)) { Fail "$id Find/Change found $($matches.Count) tabs; expected $($fields.Count - 1)." }
            for ($index = $matches.Count - 1; $index -ge 0; $index--) { $matches[$index].Contents = "`r" }
            $paragraphCount = [int]$story.Paragraphs.Count
            if ($paragraphCount -ne $fields.Count) { Fail "$id Find/Change produced $paragraphCount fields, expected $($fields.Count)." }
            for ($fieldIndex = 0; $fieldIndex -lt $paragraphCount; $fieldIndex++) {
                Apply-CreditsParagraphStyle $story.Paragraphs.Item($fieldIndex + 1) $style (Get-CreditsNestedStyle $Document $fieldIndex $id) $id
            }
            [void](Fit-CreditsToFixedFrame $frame $id)
        }
    } finally {
        try { if ($null -ne $findPreferences) { $findPreferences.FindWhat = '' } } catch {}
    }
    foreach ($row in $batch) { [void](Assert-CaptionBlock $Document $row $grouped $style $byLabel) }
    return $batch
}
function Get-GraphicBounds($Frame, [string]$Description) {
    if ([int]$Frame.AllGraphics.Count -ne 1) { Fail "$Description must contain exactly one placed graphic." }
    $bounds = @($Frame.AllGraphics.Item(1).GeometricBounds)
    if ($bounds.Count -ne 4) { Fail "$Description does not expose four graphic bounds." }
    $result = @()
    foreach ($value in $bounds) {
        $number = [double]$value
        if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) { Fail "$Description has invalid graphic bounds." }
        $result += $number
    }
    return @($result)
}
function Get-PageItemBounds($Item, [string]$Description) {
    $bounds = @($Item.GeometricBounds)
    if ($bounds.Count -ne 4) { Fail "$Description does not expose four geometric bounds." }
    $result = @()
    foreach ($value in $bounds) {
        $number = [double]$value
        if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) { Fail "$Description has invalid geometric bounds." }
        $result += $number
    }
    if ($result[2] -le $result[0] -or $result[3] -le $result[1]) { Fail "$Description has non-positive geometric bounds." }
    return @($result)
}
function Get-PageItemRotation($Item, [string]$Description) {
    try { $rotation = [double]$Item.AbsoluteRotationAngle } catch { Fail "$Description does not expose a safe rotation angle." }
    if ([double]::IsNaN($rotation) -or [double]::IsInfinity($rotation)) { Fail "$Description has an invalid rotation angle." }
    return $rotation
}
function Set-FrameGraphic($Frame, [string]$SourcePath, [string]$ExpectedFilename, [string]$Description, [bool]$ResetFit = $false) {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) { Fail "$Description source image is missing: $SourcePath" }
    if (-not (Test-FrameImage $Frame $ExpectedFilename $SourcePath)) {
        $Frame.Place($SourcePath, $false) | Out-Null
        $ResetFit = $true
    }
    # Composition plans contain absolute crop offsets.  Even when the same
    # file is already linked, reset its content to the fixed frame before
    # applying the new offset so repeated visual retries never accumulate
    # motion or push the image outside its container.
    if ($ResetFit) {
        $Frame.Fit($FIT_FILL_PROPORTIONALLY)
    }
    if (-not (Test-FrameImage $Frame $ExpectedFilename $SourcePath)) { Fail "$Description did not retain the required placed source and live link: $ExpectedFilename" }
}
function Get-CompositionProgress($Control, $State, $Arm, [string]$PlanHash) {
    $path = Join-Path $Control 'progress\composition.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $progress = Read-Json $path
    if ($progress.schema -ne 1 -or $progress.session_id -ne $State.session_id -or $progress.gate -ne 'visual-composition' -or $progress.nonce -ne $Arm.nonce -or $progress.composition_plan_sha256 -ne $PlanHash) { Fail 'Existing composition recovery state does not belong to this armed visual plan.' }
    if ($null -eq $progress.items) { Fail 'Composition recovery state has no item list.' }
    return $progress
}
function Write-CompositionProgress($Control, $State, $Arm, [string]$MasterPath, [string]$PlanHash, $Items) {
    $payload = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = 'visual-composition'; nonce = [string]$Arm.nonce
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $MasterPath
        composition_plan_sha256 = $PlanHash; look_count = [int]$State.look_count; completed_count = @($Items).Count; items = @($Items)
    }
    Write-Json (Join-Path $Control 'progress\composition.json') $payload
}
function Invoke-Composition([string]$ProjectPath, [int]$MaximumLooks, [bool]$Force = $false) {
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $arm = Read-Json (Join-Path $control 'arms\visual.json')
    if ($arm.session_id -ne $state.session_id -or $arm.gate -ne 'visual') { Fail 'Visual gate is not armed for this session.' }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $planPath = Join-Path $control 'visual\composition-plan.tsv'
    $captionGeometryPath = Join-Path $control 'evidence\caption-geometry.json'
    if (-not (Test-Path -LiteralPath $captionGeometryPath -PathType Leaf)) { $captionGeometryPath = '' }
    $captionGeometry = Get-CaptionGeometryEvidence $captionGeometryPath
    if ($captionGeometry.Count -ne [int]$state.look_count) { Fail 'Composition correction requires one saved captions-geometry record per look.' }
    $visualCaptionCorrectionPath = Join-Path $control 'visual\clearance-correction-plan.json'
    $visualCaptionCorrections = Get-VisualCaptionCorrections $visualCaptionCorrectionPath $state
    $visualCaptionCorrectionHash = if (Test-Path -LiteralPath $visualCaptionCorrectionPath -PathType Leaf) { Get-FileSha256 $visualCaptionCorrectionPath } else { $null }
    $planHash = Get-FileSha256 $planPath
    $plan = @(Get-CompositionPlan $control $registry)
    $progress = if ($Force) { $null } else { Get-CompositionProgress $control $state $arm $planHash }
    $items = @(); $completed = @{}
    if ($null -ne $progress) {
        foreach ($item in @($progress.items)) {
            if (-not $item.look_id -or $completed.ContainsKey([string]$item.look_id)) { Fail 'Composition recovery state contains an invalid or duplicate look.' }
            $items += $item; $completed[[string]$item.look_id] = $true
        }
        if ([int]$progress.completed_count -ne $items.Count) { Fail 'Composition recovery count does not match its recorded look items.' }
    }
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath 'Native composition correction'
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $visualCaptionCorrectionPath $state; Assert-Frames $doc $registry
        $checkpointDirectory = Join-Path $control 'checkpoints'
        [System.IO.Directory]::CreateDirectory($checkpointDirectory) | Out-Null
        $checkpoint = Join-Path $checkpointDirectory 'composition-before.indd'
        if ($null -eq $progress -and -not (Test-Path -LiteralPath $checkpoint -PathType Leaf)) { $doc.SaveACopy($checkpoint, $false) }
        $byLabel = New-LabelIndex $doc
        $captionStyle = Get-CreditsParagraphStyle $doc
        if ($Force) { $MaximumLooks = [int]$registry.Count }
        $pending = @($plan | Where-Object { -not $completed.ContainsKey([string]$_.look_id) })
        $batch = @($pending | Select-Object -First $MaximumLooks)
        foreach ($row in $batch) {
            $id = [string]$row.look_id
            $leftLabel = "LOOKBOOK_LEFT_IMAGE|$id"; $rightLabel = "LOOKBOOK_RIGHT_IMAGE|$id"
            $captionLabel = "LOOKBOOK_CREDITS|$id"
            if (-not $byLabel.ContainsKey($leftLabel) -or -not $byLabel.ContainsKey($rightLabel) -or -not $byLabel.ContainsKey($captionLabel)) { Fail "$id image or credits containers are missing." }
            $left = $byLabel[$leftLabel]; $right = $byLabel[$rightLabel]
            $caption = $byLabel[$captionLabel]
            $leftFilename = [string]$row.left_image_filename; $rightFilename = [string]$row.right_image_filename
            Set-FrameGraphic $left (Join-Path (Join-Path $projectFull ([string]$state.hires)) $leftFilename) $leftFilename "$id left frame" $true
            Set-FrameGraphic $right (Join-Path (Join-Path $projectFull ([string]$state.hires)) $rightFilename) $rightFilename "$id right frame"
            $before = @(Get-GraphicBounds $left "$id left graphic before crop adjustment")
            $shift = 0.0
            if (-not [double]::TryParse([string]$row.photo_adjustment_points, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$shift)) { Fail "$id has an invalid planned horizontal shift." }
            if ([math]::Abs($shift) -gt 0.0001) {
                # InDesign 2026 exposes GeometricBounds but throws a null COM
                # error when assigning it to a placed image. Move is its stable
                # native route; Type.Missing means "no destination", so the
                # second argument is the horizontal/vertical delta only.
                $leftGraphic = $left.AllGraphics.Item(1)
                if ($null -eq $leftGraphic) { Fail "$id left graphic disappeared before its crop adjustment." }
                $leftGraphic.Move([System.Type]::Missing, @($shift, 0))
            }
            $after = @(Get-GraphicBounds $left "$id left graphic after crop adjustment")
            if ([math]::Abs(([double]$after[0] - [double]$before[0])) -gt 0.01 -or [math]::Abs(([double]$after[2] - [double]$before[2])) -gt 0.01) { Fail "$id crop correction changed vertical graphic bounds." }
            if ([math]::Abs((([double]$after[1] - [double]$before[1]) - $shift)) -gt 0.1 -or [math]::Abs((([double]$after[3] - [double]$before[3]) - $shift)) -gt 0.1) { Fail "$id native horizontal crop shift was not applied exactly." }
            $captionBase = $captionGeometry[[string]$caption.Id]
            Restore-CaptionFrameBase $caption $captionBase $captionStyle $id
            $captionCorrection = $null
            if ($visualCaptionCorrections.ContainsKey($captionLabel)) {
                $savedBounds = @((Get-GeometryCoordinate $captionBase 'top'), (Get-GeometryCoordinate $captionBase 'left'), (Get-GeometryCoordinate $captionBase 'bottom'), (Get-GeometryCoordinate $captionBase 'right'))
                if (-not (Test-GeometryBounds -Actual $savedBounds -Expected @($visualCaptionCorrections[$captionLabel].before))) { Fail "$id visual caption correction does not start from the saved captions geometry." }
                $captionCorrection = Apply-VisualCaptionCorrection $caption $visualCaptionCorrections[$captionLabel] $captionStyle $id
            }
            $items += [ordered]@{
                look_id = $id; left_image_filename = $leftFilename; right_image_filename = $rightFilename; planned_shift_points = $shift
                before_left_graphic_bounds = @($before); after_left_graphic_bounds = @($after)
                caption_correction = $captionCorrection
            }
        }
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $visualCaptionCorrectionPath $state
        $doc.Save() | Out-Null
        Write-CompositionProgress $control $state $arm $masterPath $planHash $items
        if ($items.Count -eq $registry.Count) {
            $applied = [ordered]@{
                schema = 1; generator = 'run_lookbook_gate_com.ps1:ApplyComposition'; session_id = [string]$state.session_id; nonce = [string]$arm.nonce
                created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $masterPath
                composition_plan_sha256 = $planHash; clearance_correction_plan_sha256 = $visualCaptionCorrectionHash; items = @($items)
            }
            Assert-VisualCaptionCorrectionsApplied $doc $visualCaptionCorrections
            Write-Json (Join-Path $control 'visual\composition-applied.json') $applied
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_COMPOSITION_PASS $($items.Count)/$($registry.Count)"
        } else {
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_COMPOSITION_PENDING $($items.Count)/$($registry.Count) batch=$($batch.Count)"
        }
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\composition-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Get-CompositionDeltaBase([string]$ProjectFull, [string]$Control, $State, [string]$MasterPath) {
    $correctionPath = Join-Path $Control 'visual\clearance-correction-plan.json'
    if (-not (Test-Path -LiteralPath $correctionPath -PathType Leaf)) { Fail 'Targeted composition has no clearance-correction plan.' }
    $correction = Read-Json $correctionPath
    if ($correction.schema -ne 1 -or $correction.session_id -ne $State.session_id) { Fail 'Targeted composition correction plan belongs to another session.' }
    $relative = [string]$correction.prior_proof_archive
    if ([string]::IsNullOrWhiteSpace($relative)) { Fail 'Targeted composition correction plan has no prior proof archive.' }
    $archive = [System.IO.Path]::GetFullPath((Join-Path $ProjectFull $relative))
    $root = $ProjectFull.TrimEnd('\') + '\'
    if (-not $archive.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { Fail 'Targeted composition archive escapes the controlled project.' }
    $basePath = Join-Path $archive 'visual\composition-applied.json'
    if (-not (Test-Path -LiteralPath $basePath -PathType Leaf)) { Fail 'Targeted composition archive is missing its native baseline evidence.' }
    $base = Read-Json $basePath
    if ($base.schema -ne 1 -or $base.session_id -ne $State.session_id -or $base.generator -notin @('run_lookbook_gate_com.ps1:ApplyComposition', 'run_lookbook_gate_com.ps1:ApplyCompositionDelta')) {
        Fail 'Targeted composition baseline is not native evidence for this session.'
    }
    if ($null -eq $base.items -or @($base.items).Count -ne [int]$State.look_count) { Fail 'Targeted composition baseline does not cover every look.' }
    return $base
}
function Test-SameVisualCaptionCorrection($Recorded, $Expected) {
    if ($null -eq $Recorded -and $null -eq $Expected) { return $true }
    if ($null -eq $Recorded -or $null -eq $Expected) { return $false }
    if ([string]$Recorded.mode -ne [string]$Expected.mode -or [string]$Recorded.paragraph_alignment -ne [string]$Expected.paragraph_alignment) { return $false }
    try { return Test-GeometryBounds -Actual @($Recorded.after_frame_bounds) -Expected @($Expected.after) } catch { return $false }
}
function Write-CompositionDeltaProgress($Control, $State, $Arm, [string]$MasterPath, [string]$BaseHash, [string]$PlanHash, [string]$CorrectionHash, $Items) {
    $payload = [ordered]@{
        schema = 1; session_id = [string]$State.session_id; gate = 'visual-composition-delta'; nonce = [string]$Arm.nonce
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $MasterPath
        baseline_composition_sha256 = $BaseHash; composition_plan_sha256 = $PlanHash; clearance_correction_plan_sha256 = $CorrectionHash
        completed_looks = @($Items.Keys | Sort-Object); items = @($Items.Values)
    }
    Write-Json (Join-Path $Control 'progress\composition-delta.json') $payload
}
function Get-CompositionDeltaProgress($Control, $State, $Arm, [string]$BaseHash, [string]$PlanHash, [string]$CorrectionHash, [string]$MasterPath) {
    $path = Join-Path $Control 'progress\composition-delta.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $progress = Read-Json $path
    if ($progress.schema -ne 1 -or $progress.session_id -ne $State.session_id -or $progress.gate -ne 'visual-composition-delta' -or $progress.nonce -ne $Arm.nonce -or $progress.baseline_composition_sha256 -ne $BaseHash -or $progress.composition_plan_sha256 -ne $PlanHash -or $progress.clearance_correction_plan_sha256 -ne $CorrectionHash) {
        Fail 'Existing targeted composition progress does not belong to the current signed plan.'
    }
    if (-not (Same-Identity $progress.master (Master-Identity $MasterPath))) { Fail 'Targeted composition progress belongs to a different saved master.' }
    return $progress
}
function Invoke-CompositionDelta([string]$ProjectPath, [int]$MaximumLooks) {
    # A second clearance pass has already proved the full 50-look baseline.
    # Reapply only the signed differences in four-look transactions; all other
    # looks are checked read-only against that frozen baseline before every
    # save, and the final evidence still contains all 50 records.
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $arm = Read-Json (Join-Path $control 'arms\visual.json')
    if ($arm.session_id -ne $state.session_id -or $arm.gate -ne 'visual') { Fail 'Visual gate is not armed for targeted composition.' }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $planPath = Join-Path $control 'visual\composition-plan.tsv'
    $plan = @(Get-CompositionPlan $control $registry)
    $planHash = Get-FileSha256 $planPath
    $captionGeometryPath = Join-Path $control 'evidence\caption-geometry.json'
    $captionGeometry = Get-CaptionGeometryEvidence $captionGeometryPath
    if ($captionGeometry.Count -ne [int]$state.look_count) { Fail 'Targeted composition requires one saved captions-geometry record per look.' }
    $correctionPath = Join-Path $control 'visual\clearance-correction-plan.json'
    $correctionHash = Get-FileSha256 $correctionPath
    $visualCaptionCorrections = Get-VisualCaptionCorrections $correctionPath $state
    $base = Get-CompositionDeltaBase $projectFull $control $state $masterPath
    $correctionData = Read-Json $correctionPath
    $archiveRoot = [System.IO.Path]::GetFullPath((Join-Path $projectFull ([string]$correctionData.prior_proof_archive)))
    $baseHash = Get-FileSha256 (Join-Path $archiveRoot 'visual\composition-applied.json')
    $baseByLook = @{}
    foreach ($item in @($base.items)) {
        $id = [string]$item.look_id
        if (-not $id -or $baseByLook.ContainsKey($id)) { Fail 'Targeted composition baseline contains an invalid or duplicate look.' }
        $baseByLook[$id] = $item
    }
    $progress = Get-CompositionDeltaProgress $control $state $arm $baseHash $planHash $correctionHash $masterPath
    $deltaByLook = @{}
    if ($null -ne $progress) {
        foreach ($item in @($progress.items)) {
            $id = [string]$item.look_id
            if (-not $baseByLook.ContainsKey($id) -or $deltaByLook.ContainsKey($id)) { Fail 'Targeted composition progress contains an invalid or duplicate look.' }
            $deltaByLook[$id] = $item
        }
    } else {
        if (-not (Same-Identity $base.master (Master-Identity $masterPath))) { Fail 'Targeted composition baseline no longer matches the saved master.' }
    }
    $targets = @()
    foreach ($row in $plan) {
        $id = [string]$row.look_id; $baseItem = $baseByLook[$id]
        $planned = [double]$row.photo_adjustment_points; $baseShift = [double]$baseItem.planned_shift_points
        $captionLabel = "LOOKBOOK_CREDITS|$id"
        $expectedCaption = if ($visualCaptionCorrections.ContainsKey($captionLabel)) { $visualCaptionCorrections[$captionLabel] } else { $null }
        if (
            [string]$baseItem.left_image_filename -ne [string]$row.left_image_filename -or
            [string]$baseItem.right_image_filename -ne [string]$row.right_image_filename -or
            [math]::Abs($planned - $baseShift) -gt 0.01 -or
            -not (Test-SameVisualCaptionCorrection $baseItem.caption_correction $expectedCaption)
        ) { $targets += $row }
    }
    $remaining = @($targets | Where-Object { -not $deltaByLook.ContainsKey([string]$_.look_id) })
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath 'Targeted native composition correction'
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $correctionPath $state; Assert-Frames $doc $registry
        $byLabel = New-LabelIndex $doc; $captionStyle = Get-CreditsParagraphStyle $doc
        foreach ($row in $plan) {
            $id = [string]$row.look_id; $expected = if ($deltaByLook.ContainsKey($id)) { $deltaByLook[$id] } else { $baseByLook[$id] }
            $left = $byLabel["LOOKBOOK_LEFT_IMAGE|$id"]; $right = $byLabel["LOOKBOOK_RIGHT_IMAGE|$id"]
            if ($null -eq $left -or $null -eq $right) { Fail "$id image containers are missing during targeted composition." }
            $leftSource = Join-Path (Join-Path $projectFull ([string]$state.hires)) ([string]$row.left_image_filename)
            $rightSource = Join-Path (Join-Path $projectFull ([string]$state.hires)) ([string]$row.right_image_filename)
            if (-not (Test-FrameImage $left ([string]$row.left_image_filename) $leftSource) -or -not (Test-FrameImage $right ([string]$row.right_image_filename) $rightSource)) { Fail "$id has a changed or missing image link before targeted composition." }
            $current = @(Get-GraphicBounds $left "$id left graphic before targeted composition")
            if (-not (Test-GeometryBounds -Actual $current -Expected @($expected.after_left_graphic_bounds))) { Fail "$id left graphic differs from its last verified composition state." }
        }
        $batch = @($remaining | Select-Object -First $MaximumLooks)
        foreach ($row in $batch) {
            $id = [string]$row.look_id; $left = $byLabel["LOOKBOOK_LEFT_IMAGE|$id"]; $right = $byLabel["LOOKBOOK_RIGHT_IMAGE|$id"]; $caption = $byLabel["LOOKBOOK_CREDITS|$id"]
            if ($null -eq $left -or $null -eq $right -or $null -eq $caption) { Fail "$id image or credits containers are missing." }
            $leftFilename = [string]$row.left_image_filename; $rightFilename = [string]$row.right_image_filename
            Set-FrameGraphic $left (Join-Path (Join-Path $projectFull ([string]$state.hires)) $leftFilename) $leftFilename "$id left frame" $true
            Set-FrameGraphic $right (Join-Path (Join-Path $projectFull ([string]$state.hires)) $rightFilename) $rightFilename "$id right frame"
            $before = @(Get-GraphicBounds $left "$id left graphic before crop adjustment")
            $shift = [double]$row.photo_adjustment_points
            if ([math]::Abs($shift) -gt 0.0001) { $left.AllGraphics.Item(1).Move([System.Type]::Missing, @($shift, 0)) }
            $after = @(Get-GraphicBounds $left "$id left graphic after crop adjustment")
            if ([math]::Abs(([double]$after[0] - [double]$before[0])) -gt 0.01 -or [math]::Abs(([double]$after[2] - [double]$before[2])) -gt 0.01 -or [math]::Abs((([double]$after[1] - [double]$before[1]) - $shift)) -gt 0.1 -or [math]::Abs((([double]$after[3] - [double]$before[3]) - $shift)) -gt 0.1) { Fail "$id targeted crop shift was not applied exactly." }
            $captionBase = $captionGeometry[[string]$caption.Id]; Restore-CaptionFrameBase $caption $captionBase $captionStyle $id
            $captionCorrection = $null; $captionLabel = "LOOKBOOK_CREDITS|$id"
            if ($visualCaptionCorrections.ContainsKey($captionLabel)) { $captionCorrection = Apply-VisualCaptionCorrection $caption $visualCaptionCorrections[$captionLabel] $captionStyle $id }
            $deltaByLook[$id] = [ordered]@{ look_id = $id; left_image_filename = $leftFilename; right_image_filename = $rightFilename; planned_shift_points = $shift; before_left_graphic_bounds = @($before); after_left_graphic_bounds = @($after); caption_correction = $captionCorrection }
        }
        # A delta invocation saves at most four target looks.  Do not demand
        # geometry for a later caption target before its own transaction has
        # run; verify every completed correction now and the full set only
        # when the last target has been durably applied.
        $appliedCaptionCorrections = @{}
        foreach ($item in @($deltaByLook.Values)) {
            $id = [string]$item.look_id
            $captionLabel = "LOOKBOOK_CREDITS|$id"
            if ($null -ne $item.caption_correction -and $visualCaptionCorrections.ContainsKey($captionLabel)) {
                $appliedCaptionCorrections[$captionLabel] = $visualCaptionCorrections[$captionLabel]
            }
        }
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $correctionPath $state; Assert-VisualCaptionCorrectionsApplied $doc $appliedCaptionCorrections
        if ($deltaByLook.Count -eq $targets.Count) { Assert-VisualCaptionCorrectionsApplied $doc $visualCaptionCorrections }
        $doc.Save() | Out-Null
        Write-CompositionDeltaProgress $control $state $arm $masterPath $baseHash $planHash $correctionHash $deltaByLook
        if ($deltaByLook.Count -eq $targets.Count) {
            $items = @()
            foreach ($row in $plan) { $id = [string]$row.look_id; $items += $(if ($deltaByLook.ContainsKey($id)) { $deltaByLook[$id] } else { $baseByLook[$id] }) }
            $applied = [ordered]@{ schema = 1; generator = 'run_lookbook_gate_com.ps1:ApplyCompositionDelta'; session_id = [string]$state.session_id; nonce = [string]$arm.nonce; created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $masterPath; composition_plan_sha256 = $planHash; clearance_correction_plan_sha256 = $correctionHash; baseline_composition_sha256 = $baseHash; items = @($items) }
            Write-Json (Join-Path $control 'visual\composition-applied.json') $applied
            Remove-Item -LiteralPath (Join-Path $control 'progress\composition-delta.json') -Force -ErrorAction SilentlyContinue
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_COMPOSITION_DELTA_PASS $($targets.Count) targeted looks; $($registry.Count) records verified"
        } else {
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_COMPOSITION_DELTA_PENDING $($deltaByLook.Count)/$($targets.Count) targeted looks batch=$($batch.Count)"
        }
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\composition-delta-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Invoke-LookCalibration([string]$ProjectPath, [string]$TargetLookId) {
    # A calibration is intentionally a one-look native transaction.  It is for
    # resolving and inspecting a concrete editorial issue without reopening and
    # rewriting the other 49 correctly composed spreads.
    if ($TargetLookId -notmatch '^LOOK_\d{3}$') { Fail 'Look calibration requires an exact LOOK_### id.' }
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $arm = Read-Json (Join-Path $control 'arms\visual.json')
    if ($arm.session_id -ne $state.session_id -or $arm.gate -ne 'visual') { Fail 'Visual gate is not armed for this session.' }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $row = @($registry | Where-Object { [string]$_.look_id -eq $TargetLookId })
    if ($row.Count -ne 1) { Fail "$TargetLookId is not in the controlled registry." }
    $planPath = Join-Path $control 'visual\composition-plan.tsv'
    $plan = @(Get-CompositionPlan $control $registry)
    $planRow = @($plan | Where-Object { [string]$_.look_id -eq $TargetLookId })
    if ($planRow.Count -ne 1) { Fail "$TargetLookId is not in the controlled composition plan." }
    $captionGeometryPath = Join-Path $control 'evidence\caption-geometry.json'
    $captionGeometry = Get-CaptionGeometryEvidence $captionGeometryPath
    if ($captionGeometry.Count -ne [int]$state.look_count) { Fail 'Look calibration requires one saved captions-geometry record per look.' }
    $correctionPath = Join-Path $control 'visual\clearance-correction-plan.json'
    $corrections = Get-VisualCaptionCorrections $correctionPath $state
    $captionLabel = "LOOKBOOK_CREDITS|$TargetLookId"
    if (-not $corrections.ContainsKey($captionLabel)) { Fail "$TargetLookId has no signed credits correction to calibrate." }
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath 'Targeted look calibration'
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $correctionPath $state
        Assert-Frames $doc $registry
        $byLabel = New-LabelIndex $doc
        $leftLabel = "LOOKBOOK_LEFT_IMAGE|$TargetLookId"; $rightLabel = "LOOKBOOK_RIGHT_IMAGE|$TargetLookId"
        if (-not $byLabel.ContainsKey($leftLabel) -or -not $byLabel.ContainsKey($rightLabel) -or -not $byLabel.ContainsKey($captionLabel)) { Fail "$TargetLookId is missing an existing image or credits container." }
        $left = $byLabel[$leftLabel]; $right = $byLabel[$rightLabel]; $caption = $byLabel[$captionLabel]
        $hires = Join-Path $projectFull ([string]$state.hires)
        $leftFilename = [string]$planRow[0].left_image_filename; $rightFilename = [string]$planRow[0].right_image_filename
        Set-FrameGraphic $left (Join-Path $hires $leftFilename) $leftFilename "$TargetLookId left frame" $true
        Set-FrameGraphic $right (Join-Path $hires $rightFilename) $rightFilename "$TargetLookId right frame"
        $beforeGraphic = @(Get-GraphicBounds $left "$TargetLookId left graphic before calibration")
        $shift = 0.0
        if (-not [double]::TryParse([string]$planRow[0].photo_adjustment_points, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$shift)) { Fail "$TargetLookId has an invalid planned horizontal shift." }
        if ([math]::Abs($shift) -gt 0.0001) { $left.AllGraphics.Item(1).Move([System.Type]::Missing, @($shift, 0)) }
        $afterGraphic = @(Get-GraphicBounds $left "$TargetLookId left graphic after calibration")
        if ([math]::Abs(([double]$afterGraphic[0] - [double]$beforeGraphic[0])) -gt 0.01 -or [math]::Abs(([double]$afterGraphic[2] - [double]$beforeGraphic[2])) -gt 0.01) { Fail "$TargetLookId calibration changed the graphic vertically." }
        if ([math]::Abs((([double]$afterGraphic[1] - [double]$beforeGraphic[1]) - $shift)) -gt 0.1 -or [math]::Abs((([double]$afterGraphic[3] - [double]$beforeGraphic[3]) - $shift)) -gt 0.1) { Fail "$TargetLookId calibration did not apply the planned horizontal graphic shift exactly." }
        if (-not $captionGeometry.ContainsKey([string]$caption.Id)) { Fail "$TargetLookId has no saved credits geometry." }
        $captionBase = $captionGeometry[[string]$caption.Id]
        if (-not (Test-GeometryBounds -Actual @((Get-GeometryCoordinate $captionBase 'top'), (Get-GeometryCoordinate $captionBase 'left'), (Get-GeometryCoordinate $captionBase 'bottom'), (Get-GeometryCoordinate $captionBase 'right')) -Expected @($corrections[$captionLabel].before))) { Fail "$TargetLookId correction does not begin from the saved captions geometry." }
        $style = Get-CreditsParagraphStyle $doc
        Restore-CaptionFrameBase $caption $captionBase $style $TargetLookId
        $captionResult = Apply-VisualCaptionCorrection $caption $corrections[$captionLabel] $style $TargetLookId
        $interiorSafe = @(Assert-CaptionCorrectionInsideSafeArea $caption $TargetLookId)
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $correctionPath $state
        $doc.Save() | Out-Null
        $record = [ordered]@{
            schema = 1; generator = 'run_lookbook_gate_com.ps1:ApplyLookCalibration'; session_id = [string]$state.session_id; nonce = [string]$arm.nonce
            created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $masterPath
            look_id = $TargetLookId; composition_plan_sha256 = Get-FileSha256 $planPath; clearance_correction_plan_sha256 = Get-FileSha256 $correctionPath
            planned_shift_points = $shift; before_left_graphic_bounds = @($beforeGraphic); after_left_graphic_bounds = @($afterGraphic)
            caption_correction = $captionResult; caption_overflows = [bool]$caption.Overflows; interior_safe_bounds = @($interiorSafe)
        }
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH-mm-ssZ')
        Write-Json (Join-Path $control "visual\calibration\$TargetLookId-$stamp.json") $record
        $doc.Close($SAVE_NO); $doc = $null
        Write-Output "COM_LOOK_CALIBRATION_PASS $TargetLookId"
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\look-calibration-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Invoke-CaptionRepair([string]$ProjectPath, [int]$MaximumLooks) {
    # This is a visual-stage repair for a discovered local typography override;
    # it rewrites only existing credit stories from frozen caption-data. The
    # only permitted geometry change is a verified bottom-edge expansion of an
    # existing credits frame; it never adds, moves, narrows, widens or threads.
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $arm = Read-Json (Join-Path $control 'arms\visual.json')
    if ($arm.session_id -ne $state.session_id -or $arm.gate -ne 'visual') { Fail 'Visual gate is not armed for caption repair.' }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $captions = @(Read-Tsv (Join-Path $projectFull ([string]$state.captions)) @('look_id','type','brand','price','article'))
    $hires = Join-Path $projectFull ([string]$state.hires)
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath 'Caption repair'
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        Assert-Structure $doc $state $registry; Assert-Baseline -Document $doc -StructureEvidencePath (Join-Path $control 'evidence\structure.json') -AllowCreditExpansion $true; Assert-Frames $doc $registry; Assert-Images $doc $registry $null $hires
        $checkpointDirectory = Join-Path $control 'checkpoints'
        [System.IO.Directory]::CreateDirectory($checkpointDirectory) | Out-Null
        $checkpoint = Join-Path $checkpointDirectory 'visual-captions-before.indd'
        if (-not (Test-Path -LiteralPath $checkpoint -PathType Leaf)) { $doc.SaveACopy($checkpoint, $false) }
        # Text Find/Change can disconnect InDesign when repeated across a long
        # document. Repair in the same four-look saved transactions as the main
        # captions gate; each retry derives pending work from validated content.
        $changed = @(Apply-CaptionsBatch $doc $registry $captions $MaximumLooks)
        $doc.Save() | Out-Null
        $progress = Write-CaptionRepairProgress $control $state $arm $masterPath $doc $registry $captions
        if ([int]$progress.completed_count -eq [int]$registry.Count) {
            Assert-Captions $doc $registry $captions
            Write-CaptionGeometryEvidence $control $state $arm $masterPath $doc
            $payload = [ordered]@{ schema = 1; generator = 'run_lookbook_gate_com.ps1:RepairCaptions'; session_id = [string]$state.session_id; created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); master = Master-Identity $masterPath; repaired_looks = @($progress.completed_looks); repaired_count = [int]$progress.completed_count }
            Write-Json (Join-Path $control 'visual\caption-repair.json') $payload
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_CAPTION_REPAIR_PASS $($progress.completed_count)/$($registry.Count)"
        } else {
            $doc.Close($SAVE_NO); $doc = $null
            Write-Output "COM_CAPTION_REPAIR_PENDING $($progress.completed_count)/$($registry.Count) batch=$($changed.Count)"
        }
    } catch {
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Export-CaptionClearanceLayout([string]$ProjectPath) {
    # This is a read-only native snapshot.  Python uses the original placed
    # image plus these exact bounds, so the detector sees the photograph behind
    # the credits rather than the black credit glyphs rendered on top of it.
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registryPath = Join-Path $projectFull ([string]$state.registry)
    $captionsPath = Join-Path $projectFull ([string]$state.captions)
    $hiresPath = Join-Path $projectFull ([string]$state.hires)
    $captionGeometryPath = Join-Path $control 'evidence\caption-geometry.json'
    if (-not (Test-Path -LiteralPath $captionGeometryPath -PathType Leaf)) { $captionGeometryPath = '' }
    $visualCaptionCorrectionPath = Join-Path $control 'visual\clearance-correction-plan.json'
    $visualCaptionCorrections = Get-VisualCaptionCorrections $visualCaptionCorrectionPath $state
    $registry = @(Read-Tsv $registryPath @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $captions = @(Read-Tsv $captionsPath @('look_id','type','brand','price','article'))
    $app = New-Object -ComObject InDesign.Application
    # Unlike an editing gate, this audit is strictly read-only. It may safely
    # inspect a *saved* open master, which also prevents InDesign's automatic
    # document restore from turning the audit into a permanent false block.
    $doc = $null; $openedHere = $false
    foreach ($openDocument in $app.Documents) {
        if ((Normalize-Path ([string]$openDocument.FullName)) -eq (Normalize-Path $masterPath)) {
            if (-not $openDocument.Saved) { Fail 'Master is open with unsaved changes; save or close it before caption-clearance audit.' }
            $doc = $openDocument
            break
        }
    }
    try {
        if ($null -eq $doc) { $doc = Open-AutomationDocument $app $masterPath; $openedHere = $true }
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        Assert-Structure $doc $state $registry
        Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $visualCaptionCorrectionPath $state
        Assert-Frames $doc $registry
        Assert-VisualCaptionCorrectionsApplied $doc $visualCaptionCorrections
        Assert-Images $doc $registry $null $hiresPath
        # The captions gate itself rejects malformed or overset stories.  This
        # read-only audit deliberately keeps going and records every overset
        # frame too, so one clipped look never hides later model collisions.
        $byLabel = New-LabelIndex $doc
        $items = @()
        foreach ($row in $registry) {
            $id = [string]$row.look_id
            $leftLabel = "LOOKBOOK_LEFT_IMAGE|$id"; $captionLabel = "LOOKBOOK_CREDITS|$id"
            if (-not $byLabel.ContainsKey($leftLabel) -or -not $byLabel.ContainsKey($captionLabel)) { Fail "$id has no exact left-image or credits container for caption-clearance audit." }
            $left = $byLabel[$leftLabel]; $caption = $byLabel[$captionLabel]
            Assert-Page $left ([int]$row.indd_left_page) "$id left image"
            Assert-Page $caption ([int]$row.indd_left_page) "$id credits"
            $source = Join-Path $hiresPath ([string]$row.left_filename)
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Fail "$id left source is missing for caption-clearance audit: $source" }
            $expectedLeft = Join-Path $hiresPath ([string]$row.left_filename)
            if (-not (Test-FrameImage $left ([string]$row.left_filename) $expectedLeft)) { Fail "$id left graphic link changed or is missing before caption-clearance audit." }
            $graphic = $left.AllGraphics.Item(1)
            $graphicRotation = Get-PageItemRotation $graphic "$id left graphic"
            $captionRotation = Get-PageItemRotation $caption "$id credits frame"
            # The coordinate-to-pixel mapping is intentionally limited to the
            # approved unrotated template. A transformed object is ambiguous,
            # therefore it is a hard stop instead of a guessed inspection.
            if ([math]::Abs($graphicRotation) -gt 0.01 -or [math]::Abs($captionRotation) -gt 0.01) { Fail "$id has a rotated graphic or credits frame; caption-clearance audit cannot safely map it." }
            $rawPageBounds = @($caption.ParentPage.Bounds)
            if ($rawPageBounds.Count -eq 1 -and $rawPageBounds[0] -is [System.Array]) { $rawPageBounds = @($rawPageBounds[0]) }
            if ($rawPageBounds.Count -ne 4) { Fail "$id credits page has invalid bounds for clearance planning." }
            $pageBounds = @([double]($rawPageBounds[0]), [double]($rawPageBounds[1]), [double]($rawPageBounds[2]), [double]($rawPageBounds[3]))
            # The magenta page-margin guides are an editorial safe area, not a
            # cosmetic suggestion.  A credits frame may never be planned or
            # released outside them, even when the surrounding pixels are
            # otherwise empty studio background.
            $margins = $caption.ParentPage.MarginPreferences
            $safeTop = [double]($pageBounds[0]) + [double]($margins.Top)
            $safeLeft = [double]($pageBounds[1]) + [double]($margins.Left)
            $safeBottom = [double]($pageBounds[2]) - [double]($margins.Bottom)
            $safeRight = [double]($pageBounds[3]) - [double]($margins.Right)
            $safeBounds = @($safeTop, $safeLeft, $safeBottom, $safeRight)
            if ($safeBounds[2] -le $safeBounds[0] -or $safeBounds[3] -le $safeBounds[1]) { Fail "$id credits page has invalid safe-area margins." }
            $items += [ordered]@{
                look_id = $id; left_page = [int]$row.indd_left_page; source_filename = [string]$row.left_filename
                graphic_bounds = @(Get-GraphicBounds $left "$id left graphic")
                caption_bounds = @(Get-PageItemBounds $caption "$id credits frame")
                page_bounds = @($pageBounds)
                safe_bounds = @($safeBounds)
                credits_overflow = [bool]$caption.Overflows
                graphic_rotation = $graphicRotation; caption_rotation = $captionRotation
            }
        }
        if ($openedHere) { $doc.Close($SAVE_NO) }
        $doc = $null
        $payload = [ordered]@{
            schema = 1; generator = 'run_lookbook_gate_com.ps1:AuditCaptionClearance'
            session_id = [string]$state.session_id; created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            master = Master-Identity $masterPath; registry_sha256 = Get-FileSha256 $registryPath; captions_sha256 = Get-FileSha256 $captionsPath
            items = @($items)
        }
        Write-Json (Join-Path $control 'visual\caption-clearance-layout.json') $payload
        Write-Output "COM_CAPTION_CLEARANCE_LAYOUT_PASS $($items.Count)/$($registry.Count)"
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\caption-clearance-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($openedHere -and $null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Rebind-RevisionStructureEvidence([string]$ProjectPath) {
    # SaveACopy creates a new INDD with new internal page-item IDs.  The
    # structural proof from the reviewed source must therefore be rebound to
    # the untouched copy before the first CREDiTs transaction.  This action is
    # read-only for the document: it only validates the copied structure and
    # replaces the controller's ID/geometry snapshot.
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $statePath = Join-Path $control 'lookbook-state.json'
    $state = Read-Json $statePath
    $revision = if ($null -ne $state.PSObject.Properties['current_revision']) { [int]$state.current_revision } else { 1 }
    if ($revision -le 1) {
        Write-Output 'COM_STRUCTURE_REBIND_NOT_NEEDED initial-revision'
        return
    }
    $boundRevision = if ($null -ne $state.PSObject.Properties['structure_revision']) { [int]$state.structure_revision } else { 0 }
    if ($boundRevision -eq $revision) {
        Write-Output "COM_STRUCTURE_REBIND_NOT_NEEDED revision=$revision"
        return
    }
    $structurePath = Join-Path $control 'evidence\structure.json'
    $prior = Read-Json $structurePath
    if ($prior.schema -ne 1 -or $prior.gate -ne 'structure' -or -not $prior.passed -or $prior.session_id -ne $state.session_id -or $null -eq $prior.frame_geometry) {
        Fail 'Cannot rebind revision structure: the accepted source structure proof is missing or invalid.'
    }
    $captionEvidence = Join-Path $control 'evidence\captions.json'
    $captionProgress = Join-Path $control 'progress\captions.json'
    if ((Test-Path -LiteralPath $captionEvidence -PathType Leaf) -or (Test-Path -LiteralPath $captionProgress -PathType Leaf)) {
        Fail 'Cannot rebind revision structure after native captions work has begun.'
    }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath 'Revision structure rebind'
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Revision master opened with unsaved/recovered state.' }
        Assert-Structure $doc $state $registry
        Assert-Frames $doc $registry
        $geometry = @(Get-LockedFrameGeometry $doc)
        if ($geometry.Count -ne (3 * [int]$state.look_count)) { Fail "Revision structure geometry has $($geometry.Count) frames, expected $(3 * [int]$state.look_count)." }
        $evidence = [ordered]@{
            schema = 1; session_id = [string]$state.session_id; gate = 'structure'; passed = $true
            nonce = [string]$prior.nonce; created_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            master = Master-Identity $masterPath; page_item_count = (Get-DocumentPageItemCount $doc)
            text_frame_count = [int]$doc.TextFrames.Count; checks = @('structure', 'revision-identity-rebind')
            frame_geometry = $geometry; structure_revision = $revision; rebound_from_master = $prior.master
        }
        Write-Json $structurePath $evidence
        if ($null -ne $state.PSObject.Properties['structure_revision']) { $state.structure_revision = $revision }
        else { $state | Add-Member -NotePropertyName structure_revision -NotePropertyValue $revision }
        Write-Json $statePath $state
        $doc.Close($SAVE_NO); $doc = $null
        Write-Output "COM_STRUCTURE_REBIND_PASS revision=$revision frames=$($geometry.Count)"
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\structure-rebind-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Invoke-Gate([string]$ProjectPath, [string]$GateName) {
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $arm = Read-Json (Join-Path $control "arms\$GateName.json")
    if ($arm.session_id -ne $state.session_id -or $arm.gate -ne $GateName) { Fail "Gate $GateName is not armed for this session." }
    $masterPath = Join-Path $projectFull ([string]$state.master)
    # Force a PowerShell array even for a one-look test.  A bare function
    # return unwraps a single TSV object, making `.Count` unavailable.
    $registry = @(Read-Tsv (Join-Path $projectFull ([string]$state.registry)) @('look_id','spread_order','pdf_spread','left_filename','right_filename','indd_left_page','indd_right_page'))
    $captions = $null
    if ($GateName -in @('captions', 'release')) { $captions = Read-Tsv (Join-Path $projectFull ([string]$state.captions)) @('look_id','type','brand','price','article') }
    $app = New-Object -ComObject InDesign.Application
    Close-SavedAutomationMaster $app $masterPath "Gate $GateName"
    $doc = $null; $saved = $false
    try {
        $doc = Open-AutomationDocument $app $masterPath
        if (-not $doc.Saved) { Fail 'Master opened with unsaved/recovered state.' }
        $checkpointDirectory = Join-Path $control 'checkpoints'
        [System.IO.Directory]::CreateDirectory($checkpointDirectory) | Out-Null
        $checkpoint = Join-Path $checkpointDirectory "$GateName-before.indd"
        # Preserve the first pre-batch checkpoint for the whole resumable run.
        # Later batches are independently saved, so overwriting it would remove
        # the only clean rollback point after a COM interruption.
        if ($GateName -notin @('images', 'captions') -or -not (Test-Path -LiteralPath $checkpoint -PathType Leaf)) { $doc.SaveACopy($checkpoint, $false) }
        if ($GateName -eq 'structure') { Apply-Structure $doc $state $registry; Assert-Structure $doc $state $registry }
        if ($GateName -eq 'dates') { Assert-Baseline $doc (Join-Path $control 'evidence\structure.json'); Apply-Dates $doc $state; Assert-Dates $doc $state }
        if ($GateName -eq 'frames') { Assert-Baseline $doc (Join-Path $control 'evidence\structure.json'); Apply-Frames $doc $registry; Assert-Structure $doc $state $registry; Assert-Frames $doc $registry }
        if ($GateName -eq 'images') {
            Assert-Baseline $doc (Join-Path $control 'evidence\structure.json'); Assert-Frames $doc $registry
            $changedRows = @(Apply-ImagesBatch $doc $registry (Join-Path $projectFull ([string]$state.hires)) $BatchSize)
            $doc.Save() | Out-Null; $saved = $true
            $hires = Join-Path $projectFull ([string]$state.hires)
            $progress = Write-ImageProgress $control $state $arm $masterPath $doc $registry $hires
            if ([int]$progress.completed_count -eq [int]$Registry.Count) {
                Assert-Images $doc $registry $null $hires
                Write-Evidence $control $state $arm $GateName $masterPath $doc
                $doc.Close($SAVE_NO); $doc = $null
                Write-Output "COM_GATE_PASS images $($progress.completed_count)/$($progress.look_count)"
            } else {
                $doc.Close($SAVE_NO); $doc = $null
                Write-Output "COM_GATE_PENDING images $($progress.completed_count)/$($progress.look_count) batch=$($changedRows.Count)"
            }
            return
        }
        if ($GateName -eq 'captions') {
            Assert-Baseline -Document $doc -StructureEvidencePath (Join-Path $control 'evidence\structure.json') -AllowCreditExpansion $true; Assert-Frames $doc $registry
            # Credits are intentionally handled as small saved transactions.
            # The previous one-shot operation could stay in InDesign for an
            # hour with no progress or safe recovery point.
            $changedRows = @(Apply-CaptionsBatch $doc $registry $captions $BatchSize)
            $doc.Save() | Out-Null; $saved = $true
            $progress = Write-CaptionProgress $control $state $arm $masterPath $doc $registry $captions
            if ([int]$progress.completed_count -eq [int]$Registry.Count) {
                Assert-Captions $doc $registry $captions
                Assert-Baseline -Document $doc -StructureEvidencePath (Join-Path $control 'evidence\structure.json') -AllowCreditExpansion $true
                Write-CaptionGeometryEvidence $control $state $arm $masterPath $doc
                Write-Evidence $control $state $arm $GateName $masterPath $doc
                $doc.Close($SAVE_NO); $doc = $null
                Write-Output "COM_GATE_PASS captions $($progress.completed_count)/$($progress.look_count)"
            } else {
                $doc.Close($SAVE_NO); $doc = $null
                Write-Output "COM_GATE_PENDING captions $($progress.completed_count)/$($progress.look_count) batch=$($changedRows.Count)"
            }
            return
        }
        if ($GateName -eq 'release') {
            $compositionSources = Get-CompositionImageSources $control $registry
            $captionGeometryPath = Join-Path $control 'evidence\caption-geometry.json'
            $visualCaptionCorrectionPath = Join-Path $control 'visual\clearance-correction-plan.json'
            $visualCaptionCorrections = Get-VisualCaptionCorrections $visualCaptionCorrectionPath $state
            Assert-Structure $doc $state $registry; Assert-Baseline $doc (Join-Path $control 'evidence\structure.json') $captionGeometryPath $false $visualCaptionCorrectionPath $state; Assert-Dates $doc $state; Assert-Frames $doc $registry
            Assert-Images $doc $registry $compositionSources (Join-Path $projectFull ([string]$state.hires)); Assert-Captions $doc $registry $captions $visualCaptionCorrections; Assert-VisualCaptionCorrectionsApplied $doc $visualCaptionCorrections; Assert-Visual $control $state $masterPath
        }
        if ($GateName -ne 'release') { $doc.Save() | Out-Null; $saved = $true } else { $saved = $true }
        if ($GateName -eq 'release') { Write-Evidence $control $state $arm $GateName $masterPath $doc }
        else { Write-Evidence $control $state $arm $GateName $masterPath $doc }
        $doc.Close($SAVE_NO); $doc = $null
        Write-Output "COM_GATE_PASS $GateName"
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control "progress\$GateName-last-error.txt") -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Set-AdobePrintPdfPreferences($Preferences, [int]$Resolution, [string]$RequestedPageRange) {
    # Set both colour and grayscale paths explicitly.  A fashion lookbook is
    # normally colour, but source JPEGs or a PDF reference can contain
    # grayscale images and must obey the same requested PPI / JPEG quality.
    $Preferences.ColorBitmapCompression = $BITMAP_COMPRESSION_JPEG
    $Preferences.ColorBitmapQuality = $COMPRESSION_QUALITY_MEDIUM
    $Preferences.ColorBitmapSampling = $BICUBIC_DOWNSAMPLE
    $Preferences.ColorBitmapSamplingDPI = $Resolution
    $Preferences.GrayscaleBitmapCompression = $BITMAP_COMPRESSION_JPEG
    $Preferences.GrayscaleBitmapQuality = $COMPRESSION_QUALITY_MEDIUM
    $Preferences.GrayscaleBitmapSampling = $BICUBIC_DOWNSAMPLE
    $Preferences.GrayscaleBitmapSamplingDPI = $Resolution
    if ([string]::IsNullOrWhiteSpace($RequestedPageRange) -or $RequestedPageRange -eq 'ALL') { $Preferences.PageRange = $ALL_PAGES }
    else { $Preferences.PageRange = [string]$RequestedPageRange }
    $Preferences.ExportReaderSpreads = $false
    $Preferences.ViewPDF = $false
    $Preferences.OptimizePDF = $true
}
function Export-AdobePrintPdf([string]$ProjectPath, [string]$PdfPath, [int]$Resolution, [string]$RequestedPageRange) {
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $state = Read-Json (Join-Path $projectFull 'control\lookbook-state.json')
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $app = New-Object -ComObject InDesign.Application
    foreach ($openDocument in $app.Documents) { if ((Normalize-Path ([string]$openDocument.FullName)) -eq (Normalize-Path $masterPath)) { Fail 'Master is already open. Close it before COM PDF export.' } }
    $doc = $null
    try {
        $doc = Open-AutomationDocument $app $masterPath
        $prefs = $app.PDFExportPreferences
        Set-AdobePrintPdfPreferences $prefs $Resolution $RequestedPageRange
        $doc.Export($PRINT_PDF, $PdfPath, $false)
        $doc.Close($SAVE_NO); $doc = $null
        if (-not (Test-Path -LiteralPath $PdfPath -PathType Leaf) -or (Get-Item -LiteralPath $PdfPath).Length -lt 512) { Fail 'InDesign did not create a usable Adobe PDF.' }
        Write-Output "COM_EXPORT_PASS $PdfPath format=adobe-print jpeg=medium ppi=$Resolution pages=$RequestedPageRange"
    } catch {
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Export-AdobePrintPdfSet([string]$ProjectPath, [string]$PlanPath) {
    # One controlled InDesign document session for the five post-approval PDFs.
    # Each completed file is checkpointed immediately; verification remains in
    # Python and an interrupted rerun exports only absent destinations.
    if (-not (Test-Path -LiteralPath $PlanPath -PathType Leaf)) { Fail "Final export plan is missing: $PlanPath" }
    $projectFull = [System.IO.Path]::GetFullPath($ProjectPath)
    $control = Join-Path $projectFull 'control'
    $state = Read-Json (Join-Path $control 'lookbook-state.json')
    $masterPath = Join-Path $projectFull ([string]$state.master)
    $plan = Read-Json $PlanPath
    if ($plan.schema -ne 1 -or $plan.session_id -ne $state.session_id -or -not (Same-Identity $plan.master (Master-Identity $masterPath))) { Fail 'Final export plan belongs to another master or session.' }
    $entries = @($plan.outputs)
    if ($entries.Count -lt 1 -or $entries.Count -gt 5) { Fail 'Final export plan must contain from one to five PDF destinations.' }
    $root = $projectFull.TrimEnd('\') + '\'
    $app = New-Object -ComObject InDesign.Application
    foreach ($openDocument in $app.Documents) { if ((Normalize-Path ([string]$openDocument.FullName)) -eq (Normalize-Path $masterPath)) { Fail 'Master is already open. Close it before the final export set.' } }
    $doc = $null; $completed = @()
    try {
        $doc = Open-AutomationDocument $app $masterPath
        $prefs = $app.PDFExportPreferences
        foreach ($entry in $entries) {
            $relative = [string]$entry.path
            if ([string]::IsNullOrWhiteSpace($relative)) { Fail 'Final export plan contains an empty destination.' }
            $destination = [System.IO.Path]::GetFullPath((Join-Path $projectFull $relative))
            if (-not $destination.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { Fail 'Final export destination escapes the controlled project.' }
            $resolution = [int]$entry.raster_ppi
            if ($resolution -lt 72 -or $resolution -gt 600) { Fail "Final export resolution is invalid for $relative." }
            $range = [string]$entry.page_range
            if ([string]::IsNullOrWhiteSpace($range)) { Fail "Final export page range is missing for $relative." }
            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                if ((Get-Item -LiteralPath $destination).Length -lt 512) { Fail "Existing final PDF is incomplete: $relative" }
                $completed += [ordered]@{ path = $relative; skipped = $true; completed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
                continue
            }
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
            # Assign a scalar in each branch.  PowerShell turns an inline
            # ``if`` expression into an Object[] when it crosses the COM
            # boundary, which InDesign rejects for non-ALL gender ranges.
            Set-AdobePrintPdfPreferences $prefs $resolution $range
            $active = [ordered]@{
                path = $relative; raster_ppi = $resolution; page_range = $range; status = 'exporting'
                started_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            }
            Write-Json (Join-Path $control 'progress\final-export.json') ([ordered]@{
                schema = 1; session_id = [string]$state.session_id; master = Master-Identity $masterPath
                completed = @($completed); active = $active; updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            })
            $doc.Export($PRINT_PDF, $destination, $false)
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or (Get-Item -LiteralPath $destination).Length -lt 512) { Fail "InDesign did not create a usable final PDF: $relative" }
            $completed += [ordered]@{ path = $relative; raster_ppi = $resolution; page_range = $range; jpeg_quality = 'medium'; completed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
            Write-Json (Join-Path $control 'progress\final-export.json') ([ordered]@{
                schema = 1; session_id = [string]$state.session_id; master = Master-Identity $masterPath
                completed = @($completed); active = $null; updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            })
            Write-Output "COM_FINAL_EXPORT_CHECKPOINT $relative format=adobe-print jpeg=medium ppi=$resolution"
        }
        $doc.Close($SAVE_NO); $doc = $null
        Write-Output "COM_FINAL_EXPORT_SET_PASS $($completed.Count)/$($entries.Count)"
    } catch {
        try { $_ | Out-File -LiteralPath (Join-Path $control 'progress\final-export-last-error.txt') -Encoding utf8 -Force } catch {}
        if ($null -ne $doc) { try { $doc.Close($SAVE_NO) } catch {} }
        throw
    }
}
function Prepare-Template([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { Fail "Template source is missing: $Source" }
    if (Test-Path -LiteralPath $Destination) { Fail "Refusing to overwrite existing template: $Destination" }
    # Split-Path cannot combine -LiteralPath with -Parent in Windows PowerShell;
    # use .NET so the prepared-copy route works for paths with spaces as well.
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
    $app = New-Object -ComObject InDesign.Application
    $sourceDoc = Open-AutomationDocument $app $Source
    $prepared = $null
    try {
        $sourceDoc.SaveACopy($Destination, $false) | Out-Null; $sourceDoc.Close($SAVE_NO); $sourceDoc = $null
        $prepared = Open-AutomationDocument $app $Destination
        $labels = @{'LOOKBOOK_LEFT_IMAGE|LOOK_001'='LOOKBOOK_LEFT_IMAGE';'LOOKBOOK_RIGHT_IMAGE|LOOK_001'='LOOKBOOK_RIGHT_IMAGE';'LOOKBOOK_CREDITS|LOOK_001'='LOOKBOOK_CREDITS'}
        $changed = 0
        foreach ($item in @(Get-DocumentPageItems $prepared)) { if ($labels.ContainsKey([string]$item.Label)) { $item.Label = $labels[[string]$item.Label]; $changed++ } }
        if ($changed -ne 3) { Fail "Expected exactly three LOOK_001 work labels; changed $changed." }
        if ([int]$prepared.Pages.Count -ne 4 -or [int]$prepared.Spreads.Count -ne 3) { Fail 'Template must contain front + one two-page work spread + back.' }
        $prepared.Save(); $prepared.Close($SAVE_NO); $prepared = $null
        Write-Output "COM_TEMPLATE_PASS $Destination"
    } catch {
        if ($null -ne $sourceDoc) { try { $sourceDoc.Close($SAVE_NO) } catch {} }
        if ($null -ne $prepared) { try { $prepared.Close($SAVE_NO) } catch {} }
        throw
    }
}

try {
    if ($Action -eq 'ApplyGate') { Invoke-Gate $Project $Gate }
    elseif ($Action -eq 'ApplyComposition') { Invoke-Composition $Project $BatchSize $false }
    elseif ($Action -eq 'ApplyCompositionDelta') { Invoke-CompositionDelta $Project $BatchSize }
    elseif ($Action -eq 'ApplyLookCalibration') { Invoke-LookCalibration $Project $LookId }
    elseif ($Action -eq 'ReapplyComposition') { Invoke-Composition $Project $BatchSize $true }
    elseif ($Action -eq 'RepairCaptions') { Invoke-CaptionRepair $Project $BatchSize }
    elseif ($Action -eq 'AuditCaptionClearance') { Export-CaptionClearanceLayout $Project }
    elseif ($Action -eq 'RebindRevisionStructureEvidence') { Rebind-RevisionStructureEvidence $Project }
    elseif ($Action -eq 'ExportPdf') { Export-AdobePrintPdf $Project $Pdf $RasterResolution $PageRange }
    elseif ($Action -eq 'ExportPdfSet') { Export-AdobePrintPdfSet $Project $ExportPlan }
    elseif ($Action -eq 'PrepareTemplate') { Prepare-Template $TemplateSource $TemplateDestination }
} catch {
    # Native COM errors can be swallowed by a child PowerShell process when
    # InDesign terminates the invocation. Preserve the exact last error in the
    # project's control area so the controller can report a real cause rather
    # than appearing to stop silently.
    if (-not [string]::IsNullOrWhiteSpace($Project)) {
        try {
            $errorPath = Join-Path ([System.IO.Path]::GetFullPath($Project)) 'control\progress\native-last-error.txt'
            $_ | Out-File -LiteralPath $errorPath -Encoding utf8 -Force
        } catch {}
    }
    Write-Error "COM_GATE_BLOCKED: $($_.Exception.Message) at $($_.InvocationInfo.PositionMessage)"
    exit 2
}
