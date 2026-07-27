#target "InDesign"
/*
  Import confirmed product rows into caption frames by exact LOOK_### ID.
  caption-data.tsv columns: look_id, type, brand, price, article.
  It never creates frames or changes pages, spreads, images, or geometry.
*/
(function () {
    var APPLY = false;
    var BASE_LABEL = "LOOKBOOK_CREDITS";
    var PRICE = /^\d{1,3}( \d{3})* \u20BD$/;
    var input = File.openDialog("Select caption-data.tsv", "*.tsv");
    if (!input) return;

    function dataFromTsv(file) {
        if (!file.open("r")) throw Error("Cannot open caption data.");
        var lines = file.read().replace(/^\uFEFF/, "").split(/\r?\n/);
        file.close();
        var header = lines.shift().split("\t");
        var wanted = ["look_id", "type", "brand", "price", "article"], index = {}, result = {};
        for (var i = 0; i < header.length; i++) index[header[i]] = i;
        for (var j = 0; j < wanted.length; j++) if (index[wanted[j]] === undefined) throw Error("Caption data is missing " + wanted[j] + ".");
        for (var k = 0; k < lines.length; k++) {
            if (!lines[k]) continue;
            var cells = lines[k].split("\t"), id = cells[index.look_id];
            if (!/^LOOK_\d{3}$/.test(id)) throw Error("Invalid look ID in caption row " + (k + 2) + ".");
            var fields = [cells[index.type], cells[index.brand], cells[index.price], cells[index.article]];
            for (var f = 0; f < fields.length; f++) if (!fields[f]) throw Error(id + ": empty product field.");
            if (!PRICE.test(fields[2])) throw Error(id + ": price must be formatted as 123 456 rubles.");
            if (!result[id]) result[id] = [];
            result[id].push(fields);
        }
        return result;
    }

    var data = dataFromTsv(input), doc = app.activeDocument, targets = [], seen = {};
    var creditStyle = doc.paragraphStyles.itemByName("CREDiTs");
    if (!creditStyle.isValid) throw Error("Required CREDiTs paragraph style was not found.");
    for (var i = 0; i < doc.textFrames.length; i++) {
        var frame = doc.textFrames[i], match;
        if (!frame.isValid || !(match = new RegExp("^" + BASE_LABEL + "\\|(LOOK_\\d{3})$").exec(frame.label))) continue;
        var id = match[1];
        if (seen[id]) throw Error(id + ": duplicate caption-frame label.");
        if (frame.parentPage === null || frame.parentStory.textContainers.length !== 1) throw Error(id + ": frame is off-page or threaded.");
        if (!data[id] || !data[id].length) throw Error(id + ": no confirmed caption rows.");
        seen[id] = true;
        targets.push({ id: id, frame: frame, rows: data[id] });
    }
    for (var key in data) if (!seen[key]) throw Error(key + ": no matching labelled caption frame.");
    if (!targets.length) throw Error("No ID-labelled caption frames found.");

    var report = [], replacement = {}, uniqueBlocks = {};
    for (var t = 0; t < targets.length; t++) {
        var fields = [];
        for (var r = 0; r < targets[t].rows.length; r++) for (var c = 0; c < targets[t].rows[r].length; c++) fields.push(targets[t].rows[r][c]);
        replacement[targets[t].id] = fields.join("\t");
        uniqueBlocks[replacement[targets[t].id]] = true;
        report.push(targets[t].id + ": " + targets[t].rows.length + " product rows");
    }
    var distinctCount = 0;
    for (var block in uniqueBlocks) if (uniqueBlocks.hasOwnProperty(block)) distinctCount++;
    if (targets.length > 1 && distinctCount < 2)
        throw Error("All imported credit blocks are identical. Check Excel mappings before applying.");
    if (!APPLY) {
        alert("Dry run — no text changed:\r" + report.join("\r"));
        return;
    }
    app.doScript(function () {
        for (var z = 0; z < targets.length; z++) {
            var story = targets[z].frame.parentStory;
            story.contents = replacement[targets[z].id];
            app.findTextPreferences = NothingEnum.nothing;
            app.findTextPreferences.findWhat = "\t";
            var matches = story.findText();
            var expectedTabs = targets[z].rows.length * 4 - 1;
            if (matches.length !== expectedTabs) throw Error(targets[z].id + ": expected " + expectedTabs + " tabs before Find/Change.");
            for (var m = matches.length - 1; m >= 0; m--) matches[m].contents = "\r";
            for (var p = 0; p < story.paragraphs.length; p++) story.paragraphs[p].appliedParagraphStyle = creditStyle;
            app.findTextPreferences = NothingEnum.nothing;
        }
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Import normalized lookbook captions by ID");
    alert("Imported:\r" + report.join("\r"));
}());
