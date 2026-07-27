#target "InDesign"
/* Replace images only in exact LOOKBOOK_*_IMAGE|LOOK_### frames. Never changes pages. */
(function () {
    var APPLY = false;
    var input = File.openDialog("Select ready look-register.tsv", "*.tsv");
    if (!input) return;
    var hires = Folder.selectDialog("Select the project's hires folder");
    if (!hires) return;

    function rowsFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open registry.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/);
        file.close();
        var header = lines.shift().split("\t"), index = {}, wanted = ["look_id", "left_filename", "right_filename"], out = [], ids = {};
        for (var i = 0; i < header.length; i++) index[header[i]] = i;
        for (var j = 0; j < wanted.length; j++) if (index[wanted[j]] === undefined) throw Error("Registry is missing " + wanted[j] + ".");
        for (var k = 0; k < lines.length; k++) {
            if (!lines[k]) continue;
            var c = lines[k].split("\t"), id = c[index.look_id], left = c[index.left_filename], right = c[index.right_filename];
            if (!/^LOOK_\d{3}$/.test(id) || !left || !right || ids[id] || left === right) throw Error("Invalid or incomplete registry row " + (k + 2) + ".");
            ids[id] = true; out.push({id:id, left:left, right:right});
        }
        if (!out.length) throw Error("Registry has no looks.");
        return out;
    }
    function onlyFrame(doc, label) {
        var found = [];
        for (var i = 0; i < doc.allPageItems.length; i++) {
            var item = doc.allPageItems[i];
            if (item.isValid && item.parentPage && item.label === label) found.push(item);
        }
        if (found.length !== 1) throw Error(label + ": expected exactly one frame; found " + found.length + ".");
        if (found[0].parentPage === null) throw Error(label + ": frame is not on a document page.");
        try { if (found[0].graphics.length > 1) throw Error(label + ": frame has multiple graphics."); } catch (e) { throw e; }
        return found[0];
    }

    var doc = app.activeDocument, rows = rowsFromTsv(input), targets = [], seen = {};
    for (var r = 0; r < rows.length; r++) {
        var leftFile = File(hires.fsName + "/" + rows[r].left), rightFile = File(hires.fsName + "/" + rows[r].right);
        if (!leftFile.exists || !rightFile.exists) throw Error(rows[r].id + ": source file missing.");
        var left = onlyFrame(doc, "LOOKBOOK_LEFT_IMAGE|" + rows[r].id), right = onlyFrame(doc, "LOOKBOOK_RIGHT_IMAGE|" + rows[r].id);
        if (seen[left.id] || seen[right.id] || left.id === right.id) throw Error(rows[r].id + ": image frame reused.");
        seen[left.id] = true; seen[right.id] = true;
        targets.push({id:rows[r].id, frame:left, file:leftFile}); targets.push({id:rows[r].id, frame:right, file:rightFile});
    }
    for (var n = 0; n < doc.allPageItems.length; n++) {
        var item = doc.allPageItems[n];
        if (item.isValid && item.parentPage && /^LOOKBOOK_(LEFT|RIGHT)_IMAGE\|LOOK_\d{3}$/.test(item.label) && !seen[item.id]) throw Error("An ID-labelled image frame is not represented in the registry.");
    }
    var report = [];
    for (var q = 0; q < targets.length; q++) report.push(targets[q].id + " -> page " + targets[q].frame.parentPage.name + ": " + targets[q].file.name);
    if (!APPLY) { alert("Dry run - no images changed:\r" + report.join("\r")); return; }
    app.doScript(function () {
        for (var z = 0; z < targets.length; z++) { targets[z].frame.place(targets[z].file); targets[z].frame.fit(FitOptions.FILL_PROPORTIONALLY); }
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Place lookbook images by ID");
    for (var y = 0; y < targets.length; y++) if (targets[y].frame.graphics.length !== 1) throw Error("Post-check failed for " + targets[y].id + ". Undo the script.");
    alert("Placed " + targets.length + " images by exact IDs.");
}());
