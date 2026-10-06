param([string]$Driver, [string]$Root)
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Driver,[ref]$tokens,[ref]$errors)
if($errors.Count){ throw 'Driver syntax errors' }
$names=@('Fail','Read-Json','Write-Json','Normalize-Path','Master-Identity','Same-Identity','Get-FileSha256','Get-TrustedCaptionLookIds','Invalidate-CaptionReceipt','Write-CaptionProgress','Get-CaptionStyleProfile','Assert-CaptionTypography','Write-NativeActivity')
foreach($definition in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    if($definition.Name -in $names){ Invoke-Expression $definition.Extent.Text }
}
$NOTHING=1851876449
$script:audits=0
function New-LabelIndex($Document){return @{}}
function Get-CompletedCaptionLookIds($Document,$Registry,$Captions,$ByLabel){$script:audits++;return @($Registry | ForEach-Object {[string]$_.look_id})}
function Must([bool]$Condition,[string]$Message){if(-not $Condition){throw $Message}}
[void][System.IO.Directory]::CreateDirectory($Root)
foreach($count in @(1,5,50)) {
    $project=Join-Path $Root "looks-$count"
    $control=Join-Path $project 'control'
    [void][System.IO.Directory]::CreateDirectory($control)
    $master=Join-Path $project 'master.indd'
    $registryFile=Join-Path $control 'registry.tsv'; $captionFile=Join-Path $control 'captions.tsv'
    [System.IO.File]::WriteAllText($master,'saved native bytes')
    [System.IO.File]::WriteAllText($registryFile,'frozen registry')
    [System.IO.File]::WriteAllText($captionFile,'frozen captions')
    $state=[pscustomobject]@{session_id='s';registry='control/registry.tsv';captions='control/captions.tsv'}
    $arm=[pscustomobject]@{nonce='n'}; $doc=[pscustomobject]@{Modified=$false}
    $registry=@(1..$count | ForEach-Object {[pscustomobject]@{look_id=('LOOK_{0:D3}' -f $_)}})
    $ids=@($registry | ForEach-Object {$_.look_id})
    $script:audits=0
    [void](Write-CaptionProgress $control $state $arm $master $doc $registry @() $ids)
    $actual=@(Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($actual.Count -eq $count -and $script:audits -eq 0) "Exact receipt should avoid a full scan ($count)"
    $stamp=(Get-Item -LiteralPath $master).LastWriteTimeUtc
    [System.IO.File]::WriteAllText($master,'edited native byte')
    (Get-Item -LiteralPath $master).LastWriteTimeUtc=$stamp
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 1) 'Edited master must trigger an audit even with restored timestamp'
    [void](Write-CaptionProgress $control $state $arm $master $doc $registry @() $ids)
    [System.IO.File]::WriteAllText($captionFile,'changed credits')
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 2) 'Changed captions must invalidate receipt'
    [void](Write-CaptionProgress $control $state $arm $master $doc $registry @() $ids)
    $doc.Modified=$true
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 3) 'Unsaved in-memory edits must invalidate receipt'
    $doc.Modified=$false; $arm.nonce='new'
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 4) 'Another arm must invalidate receipt'
    [void](Write-CaptionProgress $control $state $arm $master $doc $registry @() $ids)
    $path=Join-Path $control 'progress/captions.json'; $receipt=Read-Json $path
    $receipt.completed_looks=@('LOOK_001','LOOK_001');$receipt.completed_count=2
    Write-Json $path $receipt
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 5) 'Duplicate IDs must invalidate receipt'
    [System.IO.File]::WriteAllText($path,'broken JSON')
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 6) 'Corrupt receipt must trigger a full audit'
    [void](Write-CaptionProgress $control $state $arm $master $doc $registry @() $ids)
    Invalidate-CaptionReceipt $control $state $arm
    $invalidated=Read-Json $path
    Must ($invalidated.completed_count -eq $count -and $invalidated.validation_policy -eq 'needs-full-audit') 'Failed final audit must preserve progress but invalidate trust'
    [void](Get-TrustedCaptionLookIds $control $state $arm $master $doc $registry @())
    Must ($script:audits -eq 7) 'Final audit failure must be recoverable through a fresh content scan'
    Write-NativeActivity $control 'captions' 'verify-look' 'LOOK_001'
    Must ((Read-Json (Join-Path $control 'progress/native-activity.json')).step -eq 'verify-look') 'Current native operation must be recorded'
}
$paragraphStyle=[pscustomobject]@{SpaceBefore=1.0;SpaceAfter=2.0;Justification=3.0}
$characterStyle=[pscustomobject]@{Name='BRAND';PointSize=12.0;Leading=14.0;Tracking=$NOTHING}
$range=[pscustomobject]@{AppliedCharacterStyle=[pscustomobject]@{Name='BRAND'};PointSize=12.0;Leading=14.0;Tracking=77.0}
$collection=[pscustomobject]@{Count=1;Range=$range}
$collection | Add-Member ScriptMethod Item {param($Index) return $this.Range}
$paragraph=[pscustomobject]@{SpaceBefore=1.0;SpaceAfter=2.0;Justification=3.0;TextStyleRanges=$collection}
$profile=Get-CaptionStyleProfile $paragraphStyle $characterStyle
Assert-CaptionTypography $paragraph $paragraphStyle $characterStyle 'LOOK_001' 1 $null $profile
$range.PointSize=13.0
$blocked=$false;try{Assert-CaptionTypography $paragraph $paragraphStyle $characterStyle 'LOOK_001' 1 $null $profile}catch{$blocked=$true}
Must $blocked 'Wrong actual text size must still fail with a cached style profile'
$range.PointSize=12.0;$range.AppliedCharacterStyle.Name='PRICE'
$blocked=$false;try{Assert-CaptionTypography $paragraph $paragraphStyle $characterStyle 'LOOK_001' 1 $null $profile}catch{$blocked=$true}
Must $blocked 'Wrong actual field style must still fail'
'PASS native caption receipt, invalidation, typography and journal contracts: 1/5/50 looks; no COM or AI invoked.'
