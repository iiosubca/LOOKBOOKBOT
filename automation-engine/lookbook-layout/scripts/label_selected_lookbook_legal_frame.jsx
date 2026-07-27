#target "InDesign"

// One-time template setup. Select the existing legal-information text frame first.
(function () {
    var LABEL = "LOOKBOOK_LEGAL";

    if (app.selection.length !== 1 || !(app.selection[0] instanceof TextFrame)) {
        throw Error("Select exactly one existing legal-information text frame.");
    }

    var frame = app.selection[0];
    if (frame.parentPage === null) {
        throw Error("The selected frame must be on a document page.");
    }
    if (frame.parentStory.textContainers.length !== 1) {
        throw Error("Refusing to label a threaded text frame.");
    }

    var dates = frame.parentStory.contents.match(/\b\d{2}\.\d{2}\.\d{4}\b/g) || [];
    if (dates.length !== 1) {
        throw Error("The selected frame must contain exactly one DD.MM.YYYY date.");
    }
    if (frame.label !== "" && frame.label !== LABEL) {
        throw Error("The selected frame already has a different object label.");
    }

    frame.label = LABEL;
    alert("Legal-information frame labelled " + LABEL + ". Date found: " + dates[0]);
}());
