#target "InDesign"
/*
  Template setup:
  - Set CREDITS_STYLE_NAME to the paragraph style used for credits.
  - Assign LOOKBOOK_CREDITS to each existing caption text frame.
  - First run with APPLY = false and verify the report.
*/
(function () {
    var APPLY = false;
    var LABEL = "LOOKBOOK_CREDITS";
    // Exact case matters in the prepared automation template.
    var CREDITS_STYLE_NAME = "CREDiTs";
    var FIELDS_PER_ROW = 4;
    var doc = app.activeDocument;
    var credits = doc.paragraphStyles.itemByName(CREDITS_STYLE_NAME);
    var none = doc.characterStyles[0];
    var targets = [];

    if (!credits.isValid) {
        throw Error("Credits paragraph style was not found. No changes were made.");
    }

    for (var i = 0; i < doc.textFrames.length; i++) {
        var frame = doc.textFrames[i];
        if (!frame.isValid || frame.label.indexOf(LABEL) !== 0) continue;
        if (frame.parentPage === null || frame.parentStory.textContainers.length !== 1) {
            throw Error("Labelled caption frame is off-page or threaded. No changes were made.");
        }
        var tabCount = (frame.parentStory.contents.match(/\t/g) || []).length;
        if (tabCount === 0 || tabCount % (FIELDS_PER_ROW - 1) !== 0) {
            throw Error("Labelled caption frame has an unexpected tab count. No changes were made.");
        }
        targets.push({ frame: frame, rows: tabCount / (FIELDS_PER_ROW - 1) });
    }

    if (!targets.length) throw Error("No labelled caption frames were found. No changes were made.");

    var report = [];
    for (var j = 0; j < targets.length; j++) {
        report.push("page " + targets[j].frame.parentPage.name + ": " + targets[j].rows + " rows");
    }
    if (!APPLY) {
        alert("Dry run — no changes made:\r" + report.join("\r"));
        return;
    }

    app.doScript(function () {
        for (var k = 0; k < targets.length; k++) {
            var text = targets[k].frame.parentStory.texts[0];
            text.characters.everyItem().appliedCharacterStyle = none;
            text.paragraphs.everyItem().appliedParagraphStyle = credits;
            app.findTextPreferences = NothingEnum.nothing;
            app.findTextPreferences.findWhat = "\t";
            var matches = text.findText();
            for (var m = matches.length - 1; m >= 0; m--) matches[m].contents = "\r";
            app.findTextPreferences = NothingEnum.nothing;
        }
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Normalize labelled lookbook captions");

    alert("Completed:\r" + report.join("\r"));
}());
