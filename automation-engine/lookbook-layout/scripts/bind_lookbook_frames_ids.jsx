#target "InDesign"
/*
  Bind all pre-labelled working-frame types to exact registry IDs.
  Required template labels: LOOKBOOK_LEFT_IMAGE, LOOKBOOK_RIGHT_IMAGE,
  LOOKBOOK_CREDITS, LOOKBOOK_FRONT, LOOKBOOK_BACK, LOOKBOOK_SHOW_DATE,
  LOOKBOOK_LEGAL. This script changes labels only.
*/
(function () {
    var APPLY = false;
    var TYPES = ["LOOKBOOK_LEFT_IMAGE", "LOOKBOOK_RIGHT_IMAGE", "LOOKBOOK_CREDITS"];
    var input = File.openDialog("Select ready look-register.tsv", "*.tsv");
    if (!input) return;

    function rowsFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open registry.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/);
        file.close();
        var header = lines.shift().split("\t"), index = {}, wanted = ["look_id", "indd_left_page", "indd_right_page"], rows = [], ids = {}, pages = {};
        for (var i = 0; i < header.length; i++) index[header[i]] = i;
        for (var j = 0; j < wanted.length; j++) if (index[wanted[j]] === undefined) throw Error("Registry is missing " + wanted[j] + ".");
        for (var k = 0; k < lines.length; k++) {
            if (!lines[k]) continue;
            var cells = lines[k].split("\t"), id = cells[index.look_id], left = cells[index.indd_left_page], right = cells[index.indd_right_page];
            if (!/^LOOK_\d{3}$/.test(id) || !left || !right || ids[id] || pages[left] || pages[right] || left === right) throw Error("Invalid, duplicate, or incomplete registry row " + (k + 2) + ".");
            ids[id] = true; pages[left] = true; pages[right] = true; rows.push({id:id, left:left, right:right});
        }
        if (!rows.length) throw Error("Registry has no looks.");
        return rows;
    }
    function itemsOnDocumentPage(doc, page) {
        var found = [];
        for (var i = 0; i < doc.allPageItems.length; i++) {
            var item = doc.allPageItems[i];
            if (item.isValid && item.parentPage && item.parentPage.id === page.id) found.push(item);
        }
        return found;
    }
    function exactOnPage(doc, page, label) {
        var found = [], items = itemsOnDocumentPage(doc, page);
        for (var i = 0; i < items.length; i++) if (items[i].label === label) found.push(items[i]);
        return found;
    }
    function isExactWorkLabel(label) {
        return /^LOOKBOOK_(LEFT_IMAGE|RIGHT_IMAGE|CREDITS)\|LOOK_\d{3}$/.test(label);
    }

    var doc = app.activeDocument, rows = rowsFromTsv(input), expectedPages = rows.length * 2 + 2;
    if (doc.pages.length !== expectedPages) throw Error("Expected " + expectedPages + " pages (covers plus complete look pairs); found " + doc.pages.length + ".");
    if (exactOnPage(doc, doc.pages.firstItem(), "LOOKBOOK_FRONT").length !== 1 || exactOnPage(doc, doc.pages.lastItem(), "LOOKBOOK_BACK").length !== 1) throw Error("Front/back cover labels are missing or ambiguous.");

    /* Exact look IDs must not exist before this binder runs. A duplicated spread
       can inherit LOOK_001 and make the placer find dozens of same-ID frames. */
    var contaminated = [];
    for (var c = 0; c < doc.allPageItems.length; c++) {
        var candidate = doc.allPageItems[c];
        if (candidate.isValid && candidate.parentPage && isExactWorkLabel(candidate.label)) contaminated.push(candidate.label + " on page " + candidate.parentPage.name);
    }
    if (contaminated.length) throw Error("Exact look IDs already exist before binding. Start from a generic-labelled staging copy; do not run a legacy binder. Found: " + contaminated.join(", "));

    var targets = [], consumed = {};
    for (var r = 0; r < rows.length; r++) {
        var leftPage = doc.pages.itemByName(rows[r].left), rightPage = doc.pages.itemByName(rows[r].right);
        if (!leftPage.isValid || !rightPage.isValid) throw Error(rows[r].id + ": registry page not found.");
        var pairs = [[leftPage, TYPES[0]], [rightPage, TYPES[1]], [leftPage, TYPES[2]]];
        for (var p = 0; p < pairs.length; p++) {
            var found = exactOnPage(doc, pairs[p][0], pairs[p][1]);
            if (found.length !== 1) throw Error(rows[r].id + ": expected exactly one " + pairs[p][1] + " frame on page " + pairs[p][0].name + ".");
            if (consumed[found[0].id]) throw Error(rows[r].id + ": frame reused by two looks.");
            consumed[found[0].id] = true; targets.push({frame:found[0], label:pairs[p][1] + "|" + rows[r].id});
        }
    }
    for (var t = 0; t < TYPES.length; t++) {
        for (var q = 0; q < doc.allPageItems.length; q++) {
            var item = doc.allPageItems[q];
            if (item.isValid && item.parentPage && item.label === TYPES[t] && !consumed[item.id]) throw Error("Unbound " + TYPES[t] + " frame found outside the registry.");
        }
    }
    var report = [];
    for (var z = 0; z < targets.length; z++) report.push(targets[z].label + " -> page " + targets[z].frame.parentPage.name);
    if (!APPLY) { alert("Dry run - no labels changed:\r" + report.join("\r")); return; }
    app.doScript(function () {
        for (var x = 0; x < targets.length; x++) targets[x].frame.label = targets[x].label;
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Bind lookbook frame IDs");
    alert("Bound " + targets.length + " frames.");
}());
