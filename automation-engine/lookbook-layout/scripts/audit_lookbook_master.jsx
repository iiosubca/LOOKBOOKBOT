#target "InDesign"
/* Read-only release gate. It never changes document content or labels. */
(function () {
    var EXPECTED_LEGAL_DATE = "09.08.2026";
    var input = File.openDialog("Select ready look-register.tsv", "*.tsv");
    if (!input) return;
    function rowsFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open registry.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/); file.close();
        var header = lines.shift().split("\t"), ix = {}, rows = [], seen = {};
        for (var i = 0; i < header.length; i++) ix[header[i]] = i;
        if (ix.look_id === undefined) throw Error("Registry is missing look_id.");
        for (var j = 0; j < lines.length; j++) if (lines[j]) { var id = lines[j].split("\t")[ix.look_id]; if (!/^LOOK_\d{3}$/.test(id) || seen[id]) throw Error("Invalid registry ID."); seen[id] = true; rows.push(id); }
        if (!rows.length) throw Error("Registry has no looks."); return rows;
    }
    var doc = app.activeDocument, ids = rowsFromTsv(input), errors = [], first = doc.pages.firstItem(), last = doc.pages.lastItem(), all = doc.allPageItems;
    /* Parent-derived template items are omitted from page.allPageItems in some InDesign versions.
       Use the same document-level collection as the placement gate, then verify parent page. */
    function countLabel(page, label) { var count = 0; for (var i = 0; i < all.length; i++) if (all[i].isValid && all[i].parentPage === page && String(all[i].label) === label) count++; return count; }
    if (doc.pages.length !== ids.length * 2 + 2) errors.push("Page count is " + doc.pages.length + ", expected " + (ids.length * 2 + 2) + ".");
    if (countLabel(first, "LOOKBOOK_FRONT") !== 1) errors.push("First page is not the unique front cover.");
    if (countLabel(last, "LOOKBOOK_BACK") !== 1) errors.push("Last page is not the unique back cover.");
    for (var p = 1; p < doc.pages.length - 1; p++) if (countLabel(doc.pages[p], "LOOKBOOK_FRONT") || countLabel(doc.pages[p], "LOOKBOOK_BACK")) errors.push("Cover label found inside working pages: " + doc.pages[p].name + ".");
    for (var r = 0; r < ids.length; r++) {
        var id = ids[r], left = 0, right = 0, credits = 0;
        for (var s = 0; s < all.length; s++) {
            var label = String(all[s].label);
            if (label === "LOOKBOOK_LEFT_IMAGE|" + id) left++;
            if (label === "LOOKBOOK_RIGHT_IMAGE|" + id) right++;
            if (label === "LOOKBOOK_CREDITS|" + id) credits++;
        }
        if (left !== 1 || right !== 1 || credits !== 1) errors.push(id + ": expected one left image, right image, and credits frame.");
    }
    var legal = [];
    for (var t = 0; t < doc.textFrames.length; t++) if (doc.textFrames[t].isValid && doc.textFrames[t].label === "LOOKBOOK_LEGAL") legal.push(doc.textFrames[t]);
    if (legal.length !== 1 || legal[0].parentPage !== last) errors.push("Legal-information frame is missing or not on the last page.");
    else if (EXPECTED_LEGAL_DATE && legal[0].parentStory.contents.indexOf(EXPECTED_LEGAL_DATE) < 0) errors.push("Legal-information date does not match " + EXPECTED_LEGAL_DATE + ".");
    if (errors.length) throw Error("RELEASE BLOCKED - no PDF may be exported:\r" + errors.join("\r"));
    alert("Release audit passed: " + ids.length + " complete looks, one front cover, one back cover.");
}());
