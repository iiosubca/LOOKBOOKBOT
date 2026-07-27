#target "InDesign"
/*
  Bind verified registry IDs to pre-existing caption frames.
  The registry must contain unique look_id and indd_left_page columns.
  This script only changes object labels; it never changes text, pages, or geometry.
*/
(function () {
    var APPLY = false;
    var BASE_LABEL = "LOOKBOOK_CREDITS";
    var registerFile = File.openDialog("Select verified look-register.tsv", "*.tsv");
    if (!registerFile) return;

    function rowsFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open registry.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/);
        file.close();
        var header = lines.shift().split("\t");
        var wanted = ["look_id", "indd_left_page"];
        var index = {};
        for (var i = 0; i < header.length; i++) index[header[i]] = i;
        for (var j = 0; j < wanted.length; j++) if (index[wanted[j]] === undefined) throw Error("Registry is missing " + wanted[j] + ".");
        var result = [], ids = {};
        for (var k = 0; k < lines.length; k++) {
            if (!lines[k]) continue;
            var cells = lines[k].split("\t");
            var id = cells[index.look_id], page = cells[index.indd_left_page];
            if (!/^LOOK_\d{3}$/.test(id) || !page || ids[id]) throw Error("Invalid or duplicate registry row " + (k + 2) + ".");
            ids[id] = true;
            result.push({ id: id, page: page });
        }
        if (!result.length) throw Error("Registry has no looks.");
        return result;
    }

    var doc = app.activeDocument, rows = rowsFromTsv(registerFile), targets = [], used = {};
    for (var r = 0; r < rows.length; r++) {
        var matches = [];
        for (var i = 0; i < doc.textFrames.length; i++) {
            var frame = doc.textFrames[i];
            if (!frame.isValid || frame.label !== BASE_LABEL || frame.parentPage === null) continue;
            if (frame.parentPage.name === rows[r].page) matches.push(frame);
        }
        if (matches.length !== 1) throw Error(rows[r].id + ": expected exactly one unbound caption frame on page " + rows[r].page + ".");
        targets.push({ id: rows[r].id, frame: matches[0] });
        used[matches[0].id] = true;
    }
    for (var n = 0; n < doc.textFrames.length; n++) {
        var candidate = doc.textFrames[n];
        if (candidate.isValid && candidate.label === BASE_LABEL && !used[candidate.id]) throw Error("An unbound caption frame is not represented in the registry.");
    }

    var report = [];
    for (var q = 0; q < targets.length; q++) report.push(targets[q].id + " -> page " + targets[q].frame.parentPage.name);
    if (!APPLY) {
        alert("Dry run — no labels changed:\r" + report.join("\r"));
        return;
    }
    app.doScript(function () {
        for (var x = 0; x < targets.length; x++) targets[x].frame.label = BASE_LABEL + "|" + targets[x].id;
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Bind lookbook IDs to caption frames");
    alert("Bound:\r" + report.join("\r"));
}());
