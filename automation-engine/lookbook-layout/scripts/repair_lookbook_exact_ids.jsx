#target "InDesign"
/*
  Emergency recovery for a legacy binder that duplicated LOOK_001 exact IDs.
  Changes labels only. It never chooses frames geometrically or creates objects.
*/
(function () {
    var APPLY = false;
    var SOURCE_LOOK = "LOOK_001";
    var input = File.openDialog("Select ready look-register.tsv", "*.tsv");
    if (!input) return;

    function rowsFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open registry.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/);
        file.close();
        var header = lines.shift().split("\t"), index = {}, rows = [], ids = {}, pages = {};
        var wanted = ["look_id", "indd_left_page", "indd_right_page"];
        for (var i = 0; i < header.length; i++) index[header[i]] = i;
        for (var w = 0; w < wanted.length; w++) if (index[wanted[w]] === undefined) throw Error("Registry is missing " + wanted[w] + ".");
        for (var j = 0; j < lines.length; j++) {
            if (!lines[j]) continue;
            var cells = lines[j].split("\t"), id = cells[index.look_id], left = cells[index.indd_left_page], right = cells[index.indd_right_page];
            if (!/^LOOK_\d{3}$/.test(id) || !left || !right || ids[id] || pages[left] || pages[right] || left === right) throw Error("Invalid registry row " + (j + 2) + ".");
            ids[id] = true; pages[left] = true; pages[right] = true; rows.push({id:id, left:left, right:right});
        }
        if (!rows.length) throw Error("Registry has no looks.");
        return rows;
    }
    function pageByName(doc, name) {
        var page = doc.pages.itemByName(name);
        if (!page.isValid) throw Error("Registry page " + name + " is absent.");
        return page;
    }
    function repairableOnPage(doc, page, sourceLabel, targetLabel, type, needsGraphic) {
        var found = [];
        for (var i = 0; i < doc.allPageItems.length; i++) {
            var item = doc.allPageItems[i];
            if (!item.isValid || !item.parentPage || item.parentPage.id !== page.id) continue;
            if (item.label !== sourceLabel && item.label !== targetLabel) continue;
            if (type && item.constructor.name !== type) continue;
            if (needsGraphic && item.allGraphics.length !== 1) continue;
            found.push(item);
        }
        if (found.length !== 1) throw Error("Page " + page.name + ": expected one source or target frame for " + targetLabel + "; found " + found.length + ".");
        return found[0];
    }

    var doc = app.activeDocument, rows = rowsFromTsv(input), changes = [];
    if (doc.pages.length !== rows.length * 2 + 2) throw Error("Unexpected page count: " + doc.pages.length + ".");
    for (var r = 0; r < rows.length; r++) {
        var row = rows[r];
        changes.push({item:repairableOnPage(doc, pageByName(doc, row.left), "LOOKBOOK_LEFT_IMAGE|" + SOURCE_LOOK, "LOOKBOOK_LEFT_IMAGE|" + row.id, "Rectangle", true), label:"LOOKBOOK_LEFT_IMAGE|" + row.id});
        changes.push({item:repairableOnPage(doc, pageByName(doc, row.right), "LOOKBOOK_RIGHT_IMAGE|" + SOURCE_LOOK, "LOOKBOOK_RIGHT_IMAGE|" + row.id, "Rectangle", true), label:"LOOKBOOK_RIGHT_IMAGE|" + row.id});
        changes.push({item:repairableOnPage(doc, pageByName(doc, row.left), "LOOKBOOK_CREDITS|" + SOURCE_LOOK, "LOOKBOOK_CREDITS|" + row.id, "TextFrame", false), label:"LOOKBOOK_CREDITS|" + row.id});
    }
    var report = [];
    for (var c = 0; c < changes.length; c++) report.push((changes[c].item.label === changes[c].label ? "OK " : "SET ") + changes[c].label + " -> page " + changes[c].item.parentPage.name);
    if (!APPLY) { alert("Dry run - no labels changed:\r" + report.join("\r")); return; }
    app.doScript(function () {
        for (var x = 0; x < changes.length; x++) changes[x].item.label = changes[x].label;
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Repair duplicated exact lookbook IDs");
    alert("Repaired " + changes.length + " exact labels.");
}());
