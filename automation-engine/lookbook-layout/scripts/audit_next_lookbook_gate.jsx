#target "InDesign"
/*
  Read-only, evidence-writing audit for an armed lookbook gate.
  It never creates, deletes, moves, or edits InDesign objects.  The gate to
  audit is read from <project>/control/arms/<gate>.json, created by
  lookbook_gate.py.  A PASS report is useful only for the exact current arm.
*/
(function () {
    var SCHEMA = 1;
    var REGISTRY_FIELDS = ["look_id", "spread_order", "pdf_spread", "left_filename", "right_filename", "indd_left_page", "indd_right_page"];
    var CAPTION_FIELDS = ["look_id", "type", "brand", "price", "article"];

    function fail(message) { throw Error(message); }
    function readFile(file) {
        if (!file.exists || !file.open("r")) fail("Cannot read " + file.fsName + ".");
        var value = file.read();
        file.close();
        return value;
    }
    function jsonFile(file) {
        try { return eval("(" + readFile(file) + ")"); }
        catch (error) { fail("Invalid controller JSON: " + file.fsName + " (" + error.message + ")"); }
    }
    function esc(value) {
        return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    }
    function quote(value) { return '"' + esc(value) + '"'; }
    function fileIdentity(file) {
        return "{\"path\":" + quote(file.fsName) + ",\"name\":" + quote(file.name) + ",\"length\":" + Number(file.length) + ",\"modified_ms\":" + Number(file.modified.getTime()) + "}";
    }
    function now() {
        var value = new Date();
        function two(number) { return (number < 10 ? "0" : "") + number; }
        return value.getUTCFullYear() + "-" + two(value.getUTCMonth() + 1) + "-" + two(value.getUTCDate()) + "T" + two(value.getUTCHours()) + ":" + two(value.getUTCMinutes()) + ":" + two(value.getUTCSeconds()) + "Z";
    }
    function basename(value) { return String(value).replace(/^.*[\\\/]/, ""); }
    function indexOf(list, value) {
        for (var i = 0; i < list.length; i++) if (list[i] === value) return i;
        return -1;
    }
    function parseTsv(file, expectedHeader) {
        var text = readFile(file).replace(/^\uFEFF/, "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        var lines = text.split("\n"), header = lines.shift().split("\t"), rows = [];
        if (header.length !== expectedHeader.length) fail(file.name + ": unexpected header.");
        for (var h = 0; h < header.length; h++) if (header[h] !== expectedHeader[h]) fail(file.name + ": header must be exactly " + expectedHeader.join(", ") + ".");
        for (var i = 0; i < lines.length; i++) {
            if (!lines[i]) continue;
            var cells = lines[i].split("\t");
            if (cells.length !== expectedHeader.length) fail(file.name + ": malformed TSV row " + (i + 2) + ".");
            var row = {}, empty = false;
            for (var j = 0; j < expectedHeader.length; j++) {
                row[expectedHeader[j]] = String(cells[j]).replace(/^\s+|\s+$/g, "");
                if (!row[expectedHeader[j]]) empty = true;
            }
            if (empty) fail(file.name + ": empty field in row " + (i + 2) + ".");
            rows.push(row);
        }
        return rows;
    }
    function pageNumber(item) {
        if (!item || item.parentPage === null) return 0;
        return Number(item.parentPage.documentOffset) + 1;
    }
    function byExactLabel(doc, label) {
        var all = doc.allPageItems, result = [];
        for (var i = 0; i < all.length; i++) if (all[i].isValid && all[i].label === label) result.push(all[i]);
        return result;
    }
    function exactlyOne(doc, label, errors) {
        var result = byExactLabel(doc, label);
        if (result.length !== 1) errors.push(label + ": expected exactly one item, found " + result.length + ".");
        return result.length === 1 ? result[0] : null;
    }
    function hasLabelOnPage(page, label) {
        var all = page.allPageItems;
        for (var i = 0; i < all.length; i++) if (all[i].isValid && all[i].label === label) return true;
        return false;
    }
    function canonical(value) {
        return String(value).replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\t/g, "\n").replace(/[ \t]+\n/g, "\n").replace(/^\s+|\s+$/g, "");
    }
    function expectedCaption(rows) {
        var lines = [];
        for (var i = 0; i < rows.length; i++) {
            lines.push(rows[i].type, rows[i].brand, rows[i].price, rows[i].article);
        }
        return lines.join("\n");
    }
    function allExpectedRows(registry, expectedCount, errors) {
        if (registry.length !== expectedCount) errors.push("Registry row count is " + registry.length + ", expected " + expectedCount + ".");
        var rows = {};
        for (var i = 0; i < registry.length; i++) {
            var row = registry[i], id = "LOOK_" + ("00" + (i + 1)).slice(-3);
            if (row.look_id !== id) errors.push("Registry sequence: expected " + id + ", got " + row.look_id + ".");
            rows[row.look_id] = row;
        }
        return rows;
    }
    function auditStructure(doc, registry, state, errors) {
        var expectedPages = Number(state.expected_pages);
        if (doc.pages.length !== expectedPages) errors.push("Document has " + doc.pages.length + " pages, expected " + expectedPages + ".");
        if (!hasLabelOnPage(doc.pages[0], "LOOKBOOK_FRONT")) errors.push("First page does not contain LOOKBOOK_FRONT.");
        if (!hasLabelOnPage(doc.pages[doc.pages.length - 1], "LOOKBOOK_BACK")) errors.push("Last page does not contain LOOKBOOK_BACK.");
        for (var p = 1; p < doc.pages.length - 1; p++) {
            if (hasLabelOnPage(doc.pages[p], "LOOKBOOK_FRONT") || hasLabelOnPage(doc.pages[p], "LOOKBOOK_BACK")) errors.push("Cover label found inside the document on page " + (p + 1) + ".");
        }
        var rows = allExpectedRows(registry, Number(state.look_count), errors);
        for (var id in rows) if (rows.hasOwnProperty(id)) {
            var row = rows[id], left = Number(row.indd_left_page), right = Number(row.indd_right_page);
            if (right !== left + 1) errors.push(id + ": registry pages must be one consecutive spread (got " + left + ", " + right + ").");
            if (left < 2 || right > doc.pages.length - 1) errors.push(id + ": registry spread is outside working pages.");
        }
    }
    function auditDates(doc, state, errors) {
        var show = exactlyOne(doc, "LOOKBOOK_SHOW_DATE", errors), legal = exactlyOne(doc, "LOOKBOOK_LEGAL", errors);
        if (show) {
            if (pageNumber(show) !== 1) errors.push("LOOKBOOK_SHOW_DATE must be on page 1.");
            if (canonical(show.contents).indexOf(canonical(state.show_text)) < 0) errors.push("LOOKBOOK_SHOW_DATE does not contain the exact show text: " + state.show_text + ".");
        }
        if (legal) {
            if (pageNumber(legal) !== doc.pages.length) errors.push("LOOKBOOK_LEGAL must be on the last page.");
            if (canonical(legal.contents).indexOf(state.legal_date) < 0) errors.push("LOOKBOOK_LEGAL does not contain the required legal date: " + state.legal_date + ".");
        }
    }
    function auditNoNewFrames(doc, project, state, errors) {
        var baselineFile = new File(project.fsName + "/control/evidence/structure.json");
        if (!baselineFile.exists) { errors.push("Structure baseline evidence is absent."); return; }
        var baseline = jsonFile(baselineFile);
        if (baseline.session_id !== state.session_id || baseline.passed !== true) { errors.push("Structure baseline belongs to another session."); return; }
        if (Number(baseline.page_item_count) !== Number(doc.allPageItems.length)) errors.push("Page-item count changed after structure proof (new or deleted frame/object detected).");
        if (Number(baseline.text_frame_count) !== Number(doc.textFrames.length)) errors.push("Text-frame count changed after structure proof (new or deleted text frame detected).");
    }
    function auditFrames(doc, registry, errors) {
        var generic = ["LOOKBOOK_LEFT_IMAGE", "LOOKBOOK_RIGHT_IMAGE", "LOOKBOOK_CREDITS"];
        for (var g = 0; g < generic.length; g++) if (byExactLabel(doc, generic[g]).length) errors.push("Generic label remains: " + generic[g] + ". Bind every frame to LOOK_### first.");
        for (var i = 0; i < registry.length; i++) {
            var row = registry[i], id = row.look_id;
            var left = exactlyOne(doc, "LOOKBOOK_LEFT_IMAGE|" + id, errors);
            var right = exactlyOne(doc, "LOOKBOOK_RIGHT_IMAGE|" + id, errors);
            var credits = exactlyOne(doc, "LOOKBOOK_CREDITS|" + id, errors);
            if (left && pageNumber(left) !== Number(row.indd_left_page)) errors.push(id + ": left image frame is on page " + pageNumber(left) + ", expected " + row.indd_left_page + ".");
            if (right && pageNumber(right) !== Number(row.indd_right_page)) errors.push(id + ": right image frame is on page " + pageNumber(right) + ", expected " + row.indd_right_page + ".");
            if (credits && pageNumber(credits) !== Number(row.indd_left_page)) errors.push(id + ": credits frame is on page " + pageNumber(credits) + ", expected " + row.indd_left_page + ".");
        }
    }
    function auditImages(doc, registry, errors) {
        for (var i = 0; i < registry.length; i++) {
            var row = registry[i], id = row.look_id;
            var frames = [
                { item: exactlyOne(doc, "LOOKBOOK_LEFT_IMAGE|" + id, errors), expected: row.left_filename, side: "left" },
                { item: exactlyOne(doc, "LOOKBOOK_RIGHT_IMAGE|" + id, errors), expected: row.right_filename, side: "right" }
            ];
            for (var j = 0; j < frames.length; j++) {
                var frame = frames[j].item;
                if (!frame) continue;
                var graphics = frame.allGraphics;
                if (graphics.length !== 1) { errors.push(id + ": " + frames[j].side + " frame needs exactly one placed graphic, found " + graphics.length + "."); continue; }
                var actual = "";
                try { actual = graphics[0].itemLink.name; } catch (error) { errors.push(id + ": " + frames[j].side + " graphic is not a linked placed file."); continue; }
                if (actual !== basename(frames[j].expected)) errors.push(id + ": " + frames[j].side + " image is " + actual + ", expected " + basename(frames[j].expected) + ".");
            }
        }
    }
    function auditCaptions(doc, captions, registry, errors) {
        var data = {}, unique = {};
        var creditStyle = doc.paragraphStyles.itemByName("CREDiTs");
        if (!creditStyle.isValid) { errors.push("Required CREDiTs paragraph style is missing."); return; }
        for (var i = 0; i < captions.length; i++) {
            var row = captions[i];
            if (!data[row.look_id]) data[row.look_id] = [];
            data[row.look_id].push(row);
        }
        for (var j = 0; j < registry.length; j++) {
            var id = registry[j].look_id;
            if (!data[id] || !data[id].length) { errors.push(id + ": no caption rows in caption-data.tsv."); continue; }
            var frame = exactlyOne(doc, "LOOKBOOK_CREDITS|" + id, errors);
            if (!frame) continue;
            var actual = "";
            try { actual = frame.parentStory.contents; } catch (error) { errors.push(id + ": credits item is not a text frame."); continue; }
            if (actual.indexOf("\t") !== -1) errors.push(id + ": credits still contain tabs; Find/Change normalization was not completed.");
            var expected = expectedCaption(data[id]);
            if (canonical(actual) !== canonical(expected)) errors.push(id + ": caption text differs from caption-data.tsv.");
            for (var p = 0; p < frame.parentStory.paragraphs.length; p++) {
                if (frame.parentStory.paragraphs[p].appliedParagraphStyle.name !== creditStyle.name) { errors.push(id + ": credits do not use CREDiTs."); break; }
            }
            unique[canonical(actual)] = true;
        }
        var total = 0;
        for (var key in unique) if (unique.hasOwnProperty(key)) total++;
        if (registry.length > 1 && total < 2) errors.push("All caption blocks are identical.");
    }
    function auditReleaseVisual(project, state, master, errors) {
        var visual = new File(project.fsName + "/control/evidence/visual.json"), arm = new File(project.fsName + "/control/arms/visual.json");
        if (!visual.exists || !arm.exists) { errors.push("Visual gate evidence is absent."); return; }
        var proof = jsonFile(visual), armed = jsonFile(arm);
        if (proof.schema !== SCHEMA || proof.session_id !== state.session_id || proof.gate !== "visual" || proof.passed !== true || proof.nonce !== armed.nonce) errors.push("Visual evidence does not belong to the current session and arm.");
        if (!proof.master || Number(proof.master.length) !== Number(master.length) || Number(proof.master.modified_ms) !== Number(master.modified.getTime())) errors.push("Visual proof was made for a different saved master checkpoint.");
    }
    function writeEvidence(project, gate, state, arm, master, errors) {
        var folder = new Folder(project.fsName + "/control/evidence");
        if (!folder.exists && !folder.create()) fail("Cannot create evidence folder.");
        var output = new File(folder.fsName + "/" + gate + ".json");
        if (!output.open("w")) fail("Cannot write " + output.fsName + ".");
        var text = "{\n" +
            "  \"schema\": " + SCHEMA + ",\n" +
            "  \"session_id\": " + quote(state.session_id) + ",\n" +
            "  \"gate\": " + quote(gate) + ",\n" +
            "  \"passed\": true,\n" +
            "  \"nonce\": " + quote(arm.nonce) + ",\n" +
            "  \"created_at\": " + quote(now()) + ",\n" +
            "  \"master\": " + fileIdentity(master) + ",\n" +
            "  \"page_item_count\": " + Number(app.activeDocument.allPageItems.length) + ",\n" +
            "  \"text_frame_count\": " + Number(app.activeDocument.textFrames.length) + ",\n" +
            "  \"checks\": " + (gate === "release" ? "[\"structure\",\"dates\",\"frames\",\"images\",\"captions\",\"visual\"]" : "[" + quote(gate) + "]") + "\n" +
            "}\n";
        output.write(text);
        output.close();
    }

    try {
        if (!app.documents.length) fail("Open the saved lookbook master before auditing.");
        var doc = app.activeDocument;
        var isModified = false;
        try { isModified = doc.modified; } catch (ignore) {}
        if (isModified) fail("Save the InDesign master before auditing. Unsaved changes cannot be proven.");
        var master = doc.fullName, project = master.parent;
        var state = jsonFile(new File(project.fsName + "/control/lookbook-state.json"));
        if (state.schema !== SCHEMA) fail("Controller state schema does not match this audit script.");
        if (master.name !== basename(state.master)) fail("The active INDD is not the master registered for this session.");
        var gates = ["structure", "dates", "frames", "images", "captions", "release"], gate = null, arm = null;
        for (var a = 0; a < gates.length; a++) {
            var candidate = new File(project.fsName + "/control/arms/" + gates[a] + ".json");
            if (!candidate.exists) continue;
            var candidateValue = jsonFile(candidate);
            if (candidateValue.session_id === state.session_id && candidateValue.gate === gates[a]) { gate = gates[a]; arm = candidateValue; }
        }
        if (gate === null) fail("No InDesign gate is armed. Run lookbook_gate.py arm <project> --gate <next gate> first.");
        var registry = parseTsv(new File(project.fsName + "/" + state.registry), REGISTRY_FIELDS), errors = [];
        if (gate === "structure") auditStructure(doc, registry, state, errors);
        if (gate === "dates") { auditNoNewFrames(doc, project, state, errors); auditDates(doc, state, errors); }
        if (gate === "frames") { auditNoNewFrames(doc, project, state, errors); auditStructure(doc, registry, state, errors); auditFrames(doc, registry, errors); }
        if (gate === "images") { auditNoNewFrames(doc, project, state, errors); auditFrames(doc, registry, errors); auditImages(doc, registry, errors); }
        if (gate === "captions") { auditNoNewFrames(doc, project, state, errors); auditFrames(doc, registry, errors); auditCaptions(doc, parseTsv(new File(project.fsName + "/" + state.captions), CAPTION_FIELDS), registry, errors); }
        if (gate === "release") {
            auditStructure(doc, registry, state, errors);
            auditNoNewFrames(doc, project, state, errors);
            auditDates(doc, state, errors);
            auditFrames(doc, registry, errors);
            auditImages(doc, registry, errors);
            auditCaptions(doc, parseTsv(new File(project.fsName + "/" + state.captions), CAPTION_FIELDS), registry, errors);
            auditReleaseVisual(project, state, master, errors);
        }
        if (errors.length) fail("GATE " + gate.toUpperCase() + " FAILED:\r\r" + errors.join("\r"));
        writeEvidence(project, gate, state, arm, master, errors);
        alert("GATE " + gate.toUpperCase() + " PASS.\rEvidence written to control/evidence/" + gate + ".json\rNow run: lookbook_gate.py confirm <project> --gate " + gate);
    } catch (error) {
        alert("GATE AUDIT BLOCKED — no PASS evidence was written.\r\r" + error.message);
    }
}());
