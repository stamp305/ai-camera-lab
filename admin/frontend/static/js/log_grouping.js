// static/js/log_grouping.js
//
// Pure, DOM-free helpers for grouping a branch's log files into folders by
// "log name" (e.g. camera_service.log, camera_service.log.2026-09-12, and
// camera_service.log.1 all belong to the same "camera_service" group), and
// for ordering files inside a group newest-first. Never hardcodes a service
// or app name: new apps just add new *.log files and fall out of the same
// generic regexes.
//
// Loaded by log_dashboard.html as a classic (non-module) <script>, *before*
// log_dashboard.js, so `LogGrouping` is a plain global by the time the
// dashboard script runs. The `var LogGrouping = ...` assignment below is
// intentionally at the top level (not inside the IIFE) so this file is also
// directly `eval()`-able from a plain JavaScriptCore context that has no
// `window` (e.g. a unit-test harness) — evaluating the file is enough to
// make `LogGrouping` available in the caller's scope.

var LogGrouping = (function () {
    'use strict';

    // Splits a posix-style path into { dir, base }; dir has no trailing slash.
    function splitPath(path) {
        var str = path === null || path === undefined ? '' : String(path);
        var idx = str.lastIndexOf('/');
        if (idx === -1) {
            return { dir: '', base: str };
        }
        return { dir: str.slice(0, idx), base: str.slice(idx + 1) };
    }

    // A trailing rotation marker: a numeric counter (".1", "-3") or a
    // date/datetime stamp (".2026-09-01", "_20260901", "-2026-09-01T02-00).
    // Deliberately the same shape regardless of where in the basename it
    // ends up sitting once earlier suffixes have been peeled off - see
    // computeStemInfo below.
    var ROTATION_SUFFIX_RE = /[._-](\d+|\d{4}-?\d{2}-?\d{2}(?:[T_-]?\d{2}(?:-?\d{2}){0,2})?)$/;

    // One trailing "generic", *letter-led* extension, e.g. the ".out" in
    // "worker.out", or ".log"/".txt"/".err". Requires a leading letter so it
    // never overlaps with ROTATION_SUFFIX_RE's purely-numeric suffixes
    // (".1", ".3", ...) - each suffix kind is handled by exactly one of the
    // two regexes below.
    var EXTENSION_RE = /\.[A-Za-z][A-Za-z0-9]{0,5}$/;

    // Basenames that reduce (see computeStemInfo) to nothing meaningful -
    // empty, or made up only of digits/dashes/underscores/dots, e.g. a plain
    // "2026-09-12.log" - all share this one folder per directory instead of
    // getting a folder per date.
    var DATE_GROUP_LABEL = 'ไฟล์ตามวันที่';
    var DATE_ONLY_STEM_RE = /^[0-9._-]*$/;

    // Repeatedly strips (a) a trailing rotation suffix and (b) one trailing
    // letter-led extension - in that order, as a pair - until a full pass
    // changes nothing. Some rotators chain more than one marker (a date
    // *and* a counter, or an extension that only becomes stripped once a
    // rotation suffix behind it is out of the way first), so a single pass
    // of either regex alone is not enough.
    //
    // (b) only fires when it leaves something behind (`extMatch.index > 0`)
    // so an extension is never stripped down to nothing. (a) has no such
    // guard: stripping it down to "" is intentional - see the all-digits
    // check in groupKeyFor, which treats "" the same as "only digits".
    //
    // Every iteration strictly shortens `stem`, so this always terminates;
    // the iteration cap is just defensive.
    function computeStemInfo(base) {
        var stem = base;
        var hadRotation = false;
        var guard = 0;

        while (guard < 64) {
            guard += 1;
            var changed = false;

            var rotationMatch = ROTATION_SUFFIX_RE.exec(stem);
            if (rotationMatch) {
                stem = stem.slice(0, rotationMatch.index);
                hadRotation = true;
                changed = true;
            }

            var extMatch = EXTENSION_RE.exec(stem);
            if (extMatch && extMatch.index > 0) {
                stem = stem.slice(0, extMatch.index);
                changed = true;
            }

            if (!changed) break;
        }

        return { stem: stem, hadRotation: hadRotation };
    }

    // Key that groups a live file with its rotated backups, e.g.
    // "camera_service" or "sub/dir/foo" (directory kept, never dropped).
    // Basenames that reduce to nothing identifying (empty, or only
    // digits/./-/_ , e.g. a bare "2026-09-12.log") all collapse to one
    // "ไฟล์ตามวันที่" group per directory instead of one folder per date.
    function groupKeyFor(path) {
        var parts = splitPath(path);
        var stem = computeStemInfo(parts.base).stem;
        var name = DATE_ONLY_STEM_RE.test(stem) ? DATE_GROUP_LABEL : stem;
        return (parts.dir ? parts.dir + '/' : '') + name;
    }

    // True for the live/current file of a group: current iff no rotation
    // suffix (step "a" above) was ever stripped, at any iteration - an
    // extension alone (".log", ".out", ...) does not make a file a backup.
    function isCurrentFile(path) {
        var base = splitPath(path).base;
        return !computeStemInfo(base).hadRotation;
    }

    // A file's `mtime` (session file entries from GET /sessions/<sid> may
    // carry one — see static/js/log_dashboard.js) reduced to a plain finite
    // number, or null for "no usable mtime": covers it being entirely
    // absent, explicitly null (server has no stat for it), or any other
    // non-numeric junk. sortGroupFiles below only ever compares two of these
    // when BOTH are non-null; a missing mtime on either side always falls
    // back to basename.
    function numericMtime(file) {
        var value = file && file.mtime;
        return typeof value === 'number' && isFinite(value) ? value : null;
    }

    // Orders a group's files for display: current file(s) first; then,
    // between two backups, by mtime descending (newest-modified first) when
    // BOTH have a usable numeric mtime; otherwise (either side missing an
    // mtime, or an exact mtime tie) by basename descending — same rule as
    // before mtime existed, and also the final tie-break so two equal
    // mtimes still sort consistently instead of depending on sort()
    // stability. Plain basename-descending happens to put zero-padded
    // dates/counters in newest-first order too (app.log.2 before app.log.1;
    // app.log.2026-09-01 before app.log.2026-08-25).
    //
    // isCurrentFile/splitPath are both regex-based (see computeStemInfo) and
    // are run exactly once per input file, up front, via this
    // decorate/sort/undecorate pass — never from inside the comparator,
    // which a sort of n files may invoke O(n log n) times. Does not mutate
    // the input array.
    function sortGroupFiles(files) {
        var decorated = (files || []).map(function (file) {
            return {
                file: file,
                current: isCurrentFile(file.path),
                base: splitPath(file.path).base,
                mtime: numericMtime(file),
            };
        });

        decorated.sort(function (a, b) {
            if (a.current !== b.current) {
                return a.current ? -1 : 1;
            }
            if (a.mtime !== null && b.mtime !== null && a.mtime !== b.mtime) {
                return a.mtime > b.mtime ? -1 : 1;
            }
            if (a.base !== b.base) {
                return a.base < b.base ? 1 : -1;
            }
            return 0;
        });

        return decorated.map(function (entry) { return entry.file; });
    }

    return {
        groupKeyFor: groupKeyFor,
        sortGroupFiles: sortGroupFiles,
        isCurrentFile: isCurrentFile,
    };
})();

if (typeof window !== 'undefined') {
    window.LogGrouping = LogGrouping;
}
