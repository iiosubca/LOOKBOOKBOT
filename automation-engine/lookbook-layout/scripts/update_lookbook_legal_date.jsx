#target "InDesign"

// Safe replacement for the date inside the labelled back-cover legal-information frame.
(function () {
    var APPLY = true; // Dry run verified the labelled legal frame and exact date.
    var LABEL = "LOOKBOOK_LEGAL";
    var EXPECTED_OLD_DATE = "24.07.2026";
    var NEW_DATE = "09.08.2026";
    var DATE_PATTERN = /^\d{2}\.\d{2}\.\d{4}$/;

    if (!DATE_PATTERN.test(EXPECTED_OLD_DATE) || !DATE_PATTERN.test(NEW_DATE)) {
        throw Error("Both dates must use DD.MM.YYYY.");
    }

    var doc = app.activeDocument;
    var matches = [];
    for (var i = 0; i < doc.textFrames.length; i++) {
        if (doc.textFrames[i].isValid && doc.textFrames[i].label === LABEL) {
            matches.push(doc.textFrames[i]);
        }
    }
    if (matches.length !== 1) {
        throw Error("Expected exactly one text frame labelled " + LABEL + "; found " + matches.length + ".");
    }

    var frame = matches[0];
    if (frame.parentPage === null || frame.parentStory.textContainers.length !== 1) {
        throw Error("The labelled legal-information frame must be a single, on-page text frame.");
    }

    var before = frame.parentStory.contents;
    var start = before.indexOf(EXPECTED_OLD_DATE);
    if (start < 0 || before.indexOf(EXPECTED_OLD_DATE, start + 1) !== -1) {
        throw Error("The expected old date must occur exactly once in the legal-information frame.");
    }
    var after = before.substring(0, start) + NEW_DATE + before.substring(start + EXPECTED_OLD_DATE.length);

    if (!APPLY) {
        alert("Dry run passed. No text changed.\rFrame label: " + LABEL + "\rReplace: " + EXPECTED_OLD_DATE + " -> " + NEW_DATE + "\rSet APPLY to true and run again.");
        return;
    }

    app.doScript(function () {
        if (frame.parentStory.contents !== before) {
            throw Error("The legal text changed after verification. No replacement was made.");
        }
        frame.parentStory.characters.itemByRange(start, start + EXPECTED_OLD_DATE.length - 1).contents = NEW_DATE;
    }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Update legal-information date only");

    if (frame.parentStory.contents !== after) {
        throw Error("Post-change verification failed: text beyond the date differs. Undo the script.");
    }
    alert("Legal-information date safely updated: " + EXPECTED_OLD_DATE + " -> " + NEW_DATE);
}());
