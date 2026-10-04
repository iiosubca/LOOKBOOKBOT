[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Master)
$ErrorActionPreference='Stop'
$masterPath=(Get-Item -LiteralPath $Master).FullName
$root=Split-Path -Parent $masterPath
$hires=Join-Path $root '_MAT\hires'
if (-not (Test-Path -LiteralPath $hires -PathType Container)) { throw 'Project _MAT/hires folder is missing.' }
$app=New-Object -ComObject InDesign.Application
function Assert-Closed {
    foreach ($open in $app.Documents) {
        if ([string]$open.FullName -eq $masterPath) { throw 'Save and close the master before relinking.' }
    }
}
Assert-Closed
$beforeHash=(Get-FileHash -LiteralPath $masterPath -Algorithm SHA256).Hash
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$history=Join-Path $root ('control\history\hires-relocation-'+$stamp)
$scratch=Join-Path $root ('control\work\scratch\hires-relocation-'+$stamp)
New-Item -ItemType Directory -Path $history,$scratch | Out-Null
$backup=Join-Path $history (Split-Path -Leaf $masterPath)
$staged=Join-Path $scratch (Split-Path -Leaf $masterPath)
Copy-Item -LiteralPath $masterPath -Destination $backup
Copy-Item -LiteralPath $masterPath -Destination $staged
$oldFolder=(Join-Path $root 'control\work\_mat\hires').Replace('\','/')
$newFolder=$hires.Replace('\','/')
$doc=$null
function Open-Staged {
    $prior=[int]$app.ScriptPreferences.UserInteractionLevel
    try { $app.ScriptPreferences.UserInteractionLevel=1699640946; return $app.Open($staged,$true) }
    finally { $app.ScriptPreferences.UserInteractionLevel=$prior }
}
# Read-only full text and container/graphic geometry capture. Relinking may
# change only link paths; a crop, text, label or object change rejects the copy.
$inspect=@'
(function(){
    function encode(v){
        if(v===null || v===undefined)return "null";
        if(typeof v==="string")return '"'+v.replace(/\\/g,"\\\\").replace(/"/g,'\\"').replace(/[\x00-\x1f]/g,function(c){return "\\u"+("000"+c.charCodeAt(0).toString(16)).slice(-4);})+'"';
        if(typeof v==="number" || typeof v==="boolean")return String(v);
        var parts=[];
        if(v instanceof Array){for(var j=0;j<v.length;j++)parts.push(encode(v[j]));return "["+parts.join(",")+"]";}
        for(var k in v)if(v.hasOwnProperty(k))parts.push(encode(k)+":"+encode(v[k]));return "{"+parts.join(",")+"}";
    }
    var d=app.activeDocument, r={pages:d.pages.length,spreads:d.spreads.length,items:[],links:[]}, all=d.allPageItems;
    for(var i=0;i<all.length;i++){
        var t=all[i], b=[], text="", source="";
        try {b=t.geometricBounds;}catch(e){}
        if(t.constructor.name==="TextFrame")text=String(t.contents);
        try {source=String(t.itemLink.filePath).replace(/\\/g,"/");}catch(e){}
        r.items.push({id:t.id,type:t.constructor.name,label:String(t.label),bounds:b,text:text,source:source});
    }
    for(var i=0;i<d.links.length;i++)r.links.push({id:d.links[i].id,path:String(d.links[i].filePath).replace(/\\/g,"/")});
    return encode(r);
})();
'@
function Inspect-Staged {
    if ([string]$app.ActiveDocument.FullName -ne $staged) { throw 'Wrong active document.' }
    return ([string]$app.DoScript($inspect,1246973031) | ConvertFrom-Json)
}
function Assert-Unchanged($Before,$After) {
    if ($Before.pages -ne $After.pages -or $Before.spreads -ne $After.spreads -or $Before.items.Count -ne $After.items.Count -or $Before.links.Count -ne $After.links.Count) { throw 'Document structure changed.' }
    for ($i=0;$i -lt $Before.items.Count;$i++) {
        $a=$Before.items[$i];$b=$After.items[$i]
        foreach ($key in @('type','label','text')) { if ($a.$key -cne $b.$key) { throw "Item $i changed: $key" } }
        $relocated=([string]$a.source -like ($oldFolder+'/*'))
        if ($a.id -ne $b.id -and -not $relocated) { throw "Unchanged item $i identity changed." }
        $expectedSource=$a.source
        if ($relocated) { $expectedSource=$newFolder+'/'+[System.IO.Path]::GetFileName([string]$a.source) }
        if ($b.source -ine $expectedSource) { throw "Item $i photo changed unexpectedly." }
        if ($a.bounds.Count -ne $b.bounds.Count) { throw 'Bounds schema changed.' }
        for ($j=0;$j -lt $a.bounds.Count;$j++) { if ([math]::Abs([double]$a.bounds[$j]-[double]$b.bounds[$j]) -gt 0.5) { throw "Item $i geometry changed." } }
    }
    $changed=0
    for ($i=0;$i -lt $Before.links.Count;$i++) {
        $a=$Before.links[$i];$expected=$a.path
        if ([string]$a.path -like ($oldFolder+'/*')) { $expected=$newFolder+'/'+[System.IO.Path]::GetFileName([string]$a.path); $changed++ }
        $matching=@($After.links | Where-Object {$_.path -ieq $expected})
        $expectedCount=@($Before.links | Where-Object {$_.path -ieq $a.path}).Count
        if ($matching.Count -ne $expectedCount) { throw 'Unexpected link change.' }
        if ($a.path -notlike ($oldFolder+'/*') -and @($matching | Where-Object {$_.id -eq $a.id}).Count -ne 1) { throw 'Unchanged link identity changed.' }
    }
    return $changed
}
try {
    $doc=Open-Staged
    $before=Inspect-Staged
    foreach ($link in $before.links) {
        if ([string]$link.path -like ($oldFolder+'/*')) {
            $target=Join-Path $hires ([System.IO.Path]::GetFileName([string]$link.path))
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Missing target photo: $target" }
        }
    }
    $repair='(function(){var d=app.activeDocument,old='+($oldFolder | ConvertTo-Json -Compress)+',nw='+($newFolder | ConvertTo-Json -Compress)+';var targets=[];for(var i=0;i<d.links.length;i++){var l=d.links[i],p=String(l.filePath).replace(/\\/g,"/");if(p.toLowerCase().indexOf(old.toLowerCase()+"/")===0)targets.push({link:l,file:new File(nw+"/"+p.substring(p.lastIndexOf("/")+1))});}for(var i=0;i<targets.length;i++){if(!targets[i].file.exists)throw Error("Missing target");}for(var i=0;i<targets.length;i++){targets[i].link.relink(targets[i].file);}return targets.length;})();'
    $count=[int]$app.DoScript($repair,1246973031)
    if ((Assert-Unchanged $before (Inspect-Staged)) -ne $count) { throw 'Relink count mismatch.' }
    if ($count -eq 0) {
        $before | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $history 'after.json') -Encoding UTF8
        Write-Output 'PASS: no legacy photo links; the original INDD is unchanged.'
        return
    }
    $doc.Save() | Out-Null
    $doc.Close(1852776480);$doc=$null
    $doc=Open-Staged
    $after=Inspect-Staged
    if ((Assert-Unchanged $before $after) -ne $count) { throw 'Saved relink count mismatch.' }
    $after | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $history 'after.json') -Encoding UTF8
    $doc.Close(1852776480);$doc=$null
    Assert-Closed
    if ((Get-FileHash -LiteralPath $masterPath -Algorithm SHA256).Hash -ne $beforeHash) { throw 'Original was modified during repair.' }
    Copy-Item -LiteralPath $staged -Destination $masterPath -Force
    if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $masterPath -Algorithm SHA256).Hash) { throw 'Installed master differs from verified copy.' }
    Write-Output "PASS: $count photo links moved to _MAT/hires; geometry, text, labels and counts verified after save/reopen."
    Write-Output "BACKUP: $backup"
} finally { if ($null -ne $doc) { $doc.Close(1852776480) } }
