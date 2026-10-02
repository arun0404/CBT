/**
 * ContentRenderer
 * ---------------
 * Turns one chapter of the manual JSON (schema: heloCBT-module-v1) into an
 * HTML string for the #content container.
 *
 * Why this exists as its own module
 * ---------------------------------
 * The previous inline renderer in index.html dropped every block whose
 * `type` wasn't "text", which is why `type: "table"` blocks — and the prose
 * inside `media-text` blocks — never appeared on the page. Adding an
 * `if (block.type === "table")` branch would have fixed the symptom and left
 * the next block type (svg, model, video) to fail the same silent way.
 *
 * Instead, block rendering is a REGISTRY: one pure function per block type,
 * keyed by `block.type`. Unknown types are logged once and skipped rather
 * than crashing the chapter, and adding support for a new type is a single
 * entry in `this.blockRenderers` — no changes to the traversal code.
 *
 * Output contract
 * ---------------
 * Returns HTML strings (never touches the DOM), which makes it trivially
 * unit-testable outside a browser and keeps the DOM write in exactly one
 * place (app/index.html). The strings it returns are safe to hand to
 * innerHTML: authored rich text (`data.html`) is passed through by design,
 * while all *data* values (table cells, captions, titles) are escaped.
 */
class ContentRenderer {

    /**
     * @param {object}  [options]
     * @param {string}  [options.mediaBaseUrl=""]     Prefix for image `src` paths.
     * @param {boolean} [options.renderImages=false]  Emit <img> for image /
     *        media-text blocks. Off by default because the media/ assets are
     *        not served by host.py yet — flip to true once they are.
     * @param {boolean} [options.hideGenericHeaders=true] Suppress the <thead>
     *        when every header is a placeholder like "Column 1" (the editor's
     *        default), so key/value spec tables read as spec tables.
     */
    constructor(options = {}) {

        this.mediaBaseUrl = options.mediaBaseUrl ?? "";
        this.renderImages = options.renderImages ?? false;
        this.hideGenericHeaders = options.hideGenericHeaders ?? true;

        // Block type -> renderer. The single place to extend.
        this.blockRenderers = {
            "text": (block) => this.renderTextBlock(block),
            "table": (block) => this.renderTableBlock(block),
            "image": (block) => this.renderImageBlock(block),
            "media-text": (block) => this.renderMediaTextBlock(block)
        };

        // Warn once per unknown type instead of once per block.
        this._warnedTypes = new Set();
    }

    // ==================================================
    // Chapter / section traversal
    // ==================================================

    /**
     * @param {object} chapter A chapter node from module.categories.<cat>.chapters
     * @returns {string} HTML
     */
    renderChapter(chapter) {

        if (!chapter) {
            console.warn("ContentRenderer: renderChapter called with no chapter.");
            return "";
        }

        // No chapter-title <h1> here: index.html's sub-header bar already
        // shows the chapter title, so repeating it at the top of #content
        // is pure duplication (see index.html's showSubtopic()).
        const parts = [];

        // A chapter can carry blocks directly as well as via its toc.
        parts.push(this.renderBlocks(chapter.blocks));

        (chapter.toc || []).forEach(section => {
            parts.push(this.renderSection(section, 2));
        });

        return parts.join("");
    }

    /**
     * Sections nest arbitrarily deep (1.2.2.1 ...). Heading level tracks the
     * nesting depth so the document outline stays semantically correct,
     * capped at <h6>.
     *
     * @param {object} section
     * @param {number} [level=2] Heading level for this section's title.
     * @returns {string} HTML
     */
    renderSection(section, level = 2) {

        if (!section) {
            return "";
        }

        const parts = [];

        if (section.title) {
            const tag = `h${Math.min(level, 6)}`;
            parts.push(`<${tag}>${this.escape(section.title)}</${tag}>`);
        }

        parts.push(this.renderBlocks(section.blocks));

        (section.children || []).forEach(child => {
            parts.push(this.renderSection(child, level + 1));
        });

        return parts.join("");
    }

    // ==================================================
    // Blocks
    // ==================================================

    renderBlocks(blocks) {

        if (!Array.isArray(blocks) || blocks.length === 0) {
            return "";
        }

        return blocks
            .map(block => this.renderBlock(block))
            .join("");
    }

    renderBlock(block) {

        if (!block || !block.type) {
            return "";
        }

        const renderer = this.blockRenderers[block.type];

        if (!renderer) {

            if (!this._warnedTypes.has(block.type)) {
                console.warn(`ContentRenderer: no renderer for block type "${block.type}" — skipping.`);
                this._warnedTypes.add(block.type);
            }

            return "";
        }

        try {
            return renderer(block) || "";
        }
        catch (err) {
            // One malformed block must never take the whole chapter down.
            console.error(`ContentRenderer: failed to render block ${block.id} (${block.type}).`, err);
            return "";
        }
    }

    // --------------------------------------------------
    // text
    // --------------------------------------------------

    renderTextBlock(block) {

        const html = block.data?.html;

        // Authored rich text — intentionally trusted and passed through.
        return html ? `<div class="block-text">${html}</div>` : "";
    }

    // --------------------------------------------------
    // table
    //
    // Shape:
    //   data.headers : string[]
    //   data.rows    : string[][]
    //   data.merges  : Array<{row, col, rowspan?, colspan?}>  (may be [])
    // --------------------------------------------------

    renderTableBlock(block) {

        const data = block.data || {};

        const headers = Array.isArray(data.headers) ? data.headers : [];
        const rows = Array.isArray(data.rows) ? data.rows : [];

        if (rows.length === 0 && headers.length === 0) {
            console.warn(`ContentRenderer: table block ${block.id} has no headers or rows.`);
            return "";
        }

        // Column count is driven by the widest row, so a short row can't
        // silently truncate the table.
        const columnCount = Math.max(
            headers.length,
            ...rows.map(row => (Array.isArray(row) ? row.length : 0))
        );

        const showHead =
            headers.length > 0 &&
            !(this.hideGenericHeaders && this.hasGenericHeaders(headers));

        const merges = this.buildMergeMap(data.merges, rows.length, columnCount);

        const parts = ['<div class="table-wrap">', '<table class="manual-table">'];

        if (showHead) {
            parts.push("<thead><tr>");
            for (let c = 0; c < columnCount; c++) {
                parts.push(`<th scope="col">${this.escape(headers[c] ?? "")}</th>`);
            }
            parts.push("</tr></thead>");
        }

        parts.push("<tbody>");

        rows.forEach((row, r) => {

            const cells = Array.isArray(row) ? row : [row];

            parts.push("<tr>");

            for (let c = 0; c < columnCount; c++) {

                const merge = merges.get(this.cellKey(r, c));

                // Covered by another cell's rowspan/colspan — emit nothing.
                if (merge === "covered") {
                    continue;
                }

                const value = this.escape(cells[c] ?? "");

                // A two-column table with a placeholder header row is really a
                // label/value spec sheet; promote the label cell to a <th> so
                // screen readers and the stylesheet both treat it as one.
                const isRowLabel = !showHead && columnCount === 2 && c === 0;

                const tag = isRowLabel ? "th" : "td";
                const scope = isRowLabel ? ' scope="row"' : "";

                const span = merge
                    ? `${merge.rowspan > 1 ? ` rowspan="${merge.rowspan}"` : ""}` +
                      `${merge.colspan > 1 ? ` colspan="${merge.colspan}"` : ""}`
                    : "";

                // An empty cell in a spec table is a section divider
                // ("Oil Type:"), not missing data — mark it so CSS can style it.
                const empty = value === "" ? ' class="is-empty"' : "";

                parts.push(`<${tag}${scope}${span}${empty}>${value}</${tag}>`);
            }

            parts.push("</tr>");
        });

        parts.push("</tbody>", "</table>");

        if (data.caption) {
            parts.push(`<figcaption class="table-caption">${this.escape(data.caption)}</figcaption>`);
        }

        parts.push("</div>");

        return parts.join("");
    }

    /**
     * True when every header is an editor placeholder ("Column 1", "Column 2")
     * or blank — i.e. carries no information worth rendering.
     */
    hasGenericHeaders(headers) {

        return headers.every(header => {
            const value = String(header ?? "").trim();
            return value === "" || /^column\s*\d+$/i.test(value);
        });
    }

    /**
     * Normalizes `merges` into a lookup:
     *   "r,c" -> { rowspan, colspan }   the anchor cell
     *   "r,c" -> "covered"              a cell swallowed by an anchor
     *
     * Defensive by design: an out-of-range or malformed merge is dropped with
     * a warning rather than corrupting the grid, and an empty/absent `merges`
     * array (the common case in this schema) costs one Map allocation.
     */
    buildMergeMap(merges, rowCount, columnCount) {

        const map = new Map();

        if (!Array.isArray(merges) || merges.length === 0) {
            return map;
        }

        merges.forEach(merge => {

            const row = Number(merge?.row);
            const col = Number(merge?.col);
            const rowspan = Math.max(1, Number(merge?.rowspan) || 1);
            const colspan = Math.max(1, Number(merge?.colspan) || 1);

            const inRange =
                Number.isInteger(row) && row >= 0 && row < rowCount &&
                Number.isInteger(col) && col >= 0 && col < columnCount &&
                row + rowspan <= rowCount &&
                col + colspan <= columnCount;

            if (!inRange) {
                console.warn("ContentRenderer: ignoring out-of-range table merge.", merge);
                return;
            }

            map.set(this.cellKey(row, col), { rowspan, colspan });

            for (let r = row; r < row + rowspan; r++) {
                for (let c = col; c < col + colspan; c++) {
                    if (r === row && c === col) {
                        continue;
                    }
                    map.set(this.cellKey(r, c), "covered");
                }
            }
        });

        return map;
    }

    cellKey(row, col) {
        return `${row},${col}`;
    }

    // --------------------------------------------------
    // image
    // --------------------------------------------------

    renderImageBlock(block) {

        const data = block.data || {};

        if (!this.renderImages || !data.src) {
            return "";
        }

        return this.figure(data.src, data.caption, data.width);
    }

    // --------------------------------------------------
    // media-text
    //
    // Carries authored prose in data.text.html alongside the media. The prose
    // was being dropped entirely by the old renderer, so real manual content
    // (e.g. "It transmits power/torque ... at the speed of 6000 RPM") never
    // reached the page — or the TTS reader.
    // --------------------------------------------------

    renderMediaTextBlock(block) {

        const data = block.data || {};

        const parts = ['<div class="block-media-text">'];

        const html = data.text?.html;

        if (html) {
            parts.push(`<div class="block-text">${html}</div>`);
        }

        if (this.renderImages && data.mediaType === "image" && data.image?.src) {
            parts.push(this.figure(
                data.image.src,
                data.image.caption,
                data.image.width
            ));
        }

        parts.push("</div>");

        return parts.join("");
    }

    figure(src, caption, width) {

        const url = this.escape(this.mediaBaseUrl + src);
        const style = Number(width) > 0 ? ` style="width:${Number(width)}%"` : "";

        const parts = [`<figure class="block-figure"${style}>`];

        parts.push(`<img src="${url}" alt="${this.escape(caption || "")}" loading="lazy">`);

        if (caption) {
            parts.push(`<figcaption>${this.escape(caption)}</figcaption>`);
        }

        parts.push("</figure>");

        return parts.join("");
    }

    // ==================================================
    // Helpers
    // ==================================================

    /**
     * Escapes a *data* value for HTML text context. Applied to everything that
     * is not authored rich text: table cells, captions, section titles.
     */
    escape(value) {

        if (value === null || value === undefined) {
            return "";
        }

        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }
}

// Node (tests) + browser (global, matching the existing script-tag setup).
if (typeof module !== "undefined" && module.exports) {
    module.exports = ContentRenderer;
}
