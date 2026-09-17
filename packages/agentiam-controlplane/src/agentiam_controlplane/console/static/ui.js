/* Console UI primitives. No framework, no build step — the console is served as static
 * files from the control plane itself and has to keep working on an air-gapped demo
 * machine (DEMO.md drill F-1).
 *
 * Three things live here because three pages need them:
 *   tick()    — animates a number toward a new value instead of snapping to it
 *   drawer()  — the record inspector that slides in over a live feed
 *   kv()      — builds the label/value grid both drawers are made of
 */
(function (global) {
    "use strict";

    var reduceMotion = global.matchMedia
        ? global.matchMedia("(prefers-reduced-motion: reduce)").matches
        : false;

    function format(value, decimals) {
        return Number(value).toLocaleString(undefined, {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
    }

    /* Animate `el` from whatever it currently reads to `value`.
     *
     * The element keeps the last value on a property rather than re-parsing its own
     * text: the text is formatted with separators, and parsing it back is how you get a
     * counter that reads 1,234 and animates from 1. */
    function tick(el, value, options) {
        if (!el) return;
        var opts = options || {};
        var decimals = opts.decimals === undefined ? 0 : opts.decimals;
        var target = Number(value);
        if (!isFinite(target)) {
            el.textContent = "–";
            el._tickValue = 0;
            return;
        }

        var from = typeof el._tickValue === "number" ? el._tickValue : 0;
        el._tickValue = target;
        if (from === target) {
            el.textContent = format(target, decimals);
            return;
        }
        if (reduceMotion) {
            el.textContent = format(target, decimals);
            return;
        }

        if (el._tickFrame) global.cancelAnimationFrame(el._tickFrame);

        var duration = opts.duration || 520;
        var start = null;

        function frame(now) {
            if (start === null) start = now;
            var t = Math.min(1, (now - start) / duration);
            // easeOutExpo: fast commit, long settle. A linear count looks mechanical;
            // a bouncy one looks like a toy.
            var eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
            el.textContent = format(from + (target - from) * eased, decimals);
            if (t < 1) {
                el._tickFrame = global.requestAnimationFrame(frame);
            } else {
                el._tickFrame = null;
                el.textContent = format(target, decimals);
            }
        }
        el._tickFrame = global.requestAnimationFrame(frame);
    }

    /* Wire a drawer element and its backdrop once; returns { open, close }.
     *
     * Escape closes it and focus moves to the close button, because an operator who
     * opened a record with the keyboard should not have to reach for the mouse to get
     * back to the feed. */
    function drawer(drawerEl, backdropEl) {
        if (!drawerEl) return { open: function () {}, close: function () {} };

        function close() {
            drawerEl.classList.remove("open");
            drawerEl.setAttribute("aria-hidden", "true");
            if (backdropEl) backdropEl.classList.remove("open");
            var marked = document.querySelectorAll("tr.selected");
            for (var i = 0; i < marked.length; i++) marked[i].classList.remove("selected");
        }

        function open() {
            drawerEl.classList.add("open");
            drawerEl.setAttribute("aria-hidden", "false");
            if (backdropEl) backdropEl.classList.add("open");
            var closeBtn = drawerEl.querySelector(".close");
            if (closeBtn) closeBtn.focus();
        }

        if (backdropEl) backdropEl.addEventListener("click", close);
        var closeBtn = drawerEl.querySelector(".close");
        if (closeBtn) closeBtn.addEventListener("click", close);
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && drawerEl.classList.contains("open")) close();
        });

        drawerEl.setAttribute("aria-hidden", "true");
        return { open: open, close: close };
    }

    /* Build a <dl class="kv"> body from [label, value, opts] triples. Entries whose
     * value is null/undefined/"" are dropped rather than rendered as blank rows: an
     * inspector full of empty fields hides the fields that are actually populated. */
    function kv(container, pairs) {
        if (!container) return;
        container.textContent = "";
        for (var i = 0; i < pairs.length; i++) {
            var label = pairs[i][0];
            var value = pairs[i][1];
            var opts = pairs[i][2] || {};
            if (value === null || value === undefined || value === "") continue;

            var dt = document.createElement("dt");
            dt.textContent = label;
            var dd = document.createElement("dd");
            if (opts.plain) dd.className = "plain";
            if (opts.node) {
                dd.appendChild(value);
            } else {
                dd.textContent = String(value);
            }
            container.append(dt, dd);
        }
    }

    function pill(outcome) {
        var span = document.createElement("span");
        span.className = "pill " + (outcome || "");
        span.textContent = outcome || "?";
        return span;
    }

    /* Tab switching is pure CSS — `@view-transition` plus a shared
     * `view-transition-name` on the lit pill, so the browser tweens the pill from its
     * old position to its new one across the navigation.
     *
     * There was JavaScript here that moved `aria-current` on click, to make the pill
     * respond before the new document arrived. It did the opposite: the browser
     * snapshots the outgoing page *after* the click handler runs, so the "old" pill was
     * already sitting at its destination and the morph had nothing left to animate.
     * Deleted rather than fixed — the transition needs no help, only to be left alone. */

    global.AgentIAM = { tick: tick, drawer: drawer, kv: kv, pill: pill, reduceMotion: reduceMotion };
})(window);
