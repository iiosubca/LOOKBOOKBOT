#target "InDesign"
/* Read-only structural report for an automation-template INDD. */
(function () {
    function esc(value) {
        return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    }
    function quote(value) { return '"' + esc(value) + '"'; }
    function pageNumber(item) { return item.parentPage === null ? 0 : Number(item.parentPage.documentOffset) + 1; }
    function bounds(item) {
        try {
            var value = item.geometricBounds;
            return "[" + [Number(value[0]), Number(value[1]), Number(value[2]), Number(value[3])].join(",") + "]";
        } catch (error) { return "null"; }
    }
    function typeOf(item) {
        try { return item.constructor.name; } catch (error) { return "Unknown"; }
    }
    try {
        if (!app.documents.length) throw Error("Open one template document first.");
        var doc = app.activeDocument, master = doc.fullName, pieces = [], all = doc.allPageItems;
        for (var i = 0; i < all.length; i++) {
            var item = all[i];
            if (!item.isValid) continue;
            var graphics = 0, link = "", text = "", threaded = false;
            try { graphics = item.allGraphics.length; } catch (ignoreGraphics) {}
            try { if (graphics === 1) link = item.allGraphics[0].itemLink.name; } catch (ignoreLink) {}
            try { text = item.contents; } catch (ignoreText) {}
            try { threaded = item.parentStory.textContainers.length > 1; } catch (ignoreThread) {}
            pieces.push("{\"page\":" + pageNumber(item) + ",\"type\":" + quote(typeOf(item)) + ",\"label\":" + quote(item.label || "") + ",\"name\":" + quote(item.name || "") + ",\"bounds\":" + bounds(item) + ",\"graphics\":" + graphics + ",\"link\":" + quote(link) + ",\"threaded\":" + (threaded ? "true" : "false") + ",\"text_preview\":" + quote(String(text || "").substring(0, 180)) + "}");
        }
        var report = "{\n" +
            "  \"document\": " + quote(master.fsName) + ",\n" +
            "  \"pages\": " + doc.pages.length + ",\n" +
            "  \"spreads\": " + doc.spreads.length + ",\n" +
            "  \"text_frames\": " + doc.textFrames.length + ",\n" +
            "  \"page_items\": " + all.length + ",\n" +
            "  \"facing_pages\": " + (doc.documentPreferences.facingPages ? "true" : "false") + ",\n" +
            "  \"items\": [\n    " + pieces.join(",\n    ") + "\n  ]\n}\n";
        var output = new File(master.parent.fsName + "/template-automation-inspection.json");
        if (!output.open("w")) throw Error("Cannot write " + output.fsName + ".");
        output.write(report);
        output.close();
        alert("TEMPLATE INSPECTION PASS\r" + output.fsName + "\rNo InDesign objects were changed.");
    } catch (error) {
        alert("TEMPLATE INSPECTION BLOCKED\r" + error.message);
    }
}());
