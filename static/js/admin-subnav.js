// Sections INSIDE an admin tab (Admin → Relay: General · Access & identity · Limits · …).
//
// Markup: a <nav class="admin-subnav" data-subnav="TAB"> of <button class="subnav-btn" data-sec="NAME"> as the first
// child of the tab's .tab-content, and one <section class="admin-sec" data-sec="NAME"> per section beside it.
// Without this script every section simply shows (the CSS only hides once `.subnav-on` is set), so nothing is ever
// unreachable. Sections are a VIEW: every input stays in the one settings form, so Save sends a changed field
// whichever section it is in, and admin.js hydrates every field by id exactly as before.
//
// The open section is remembered per tab (localStorage, like the tab itself in admin.js) and can be deep-linked as
// #tab-relay/limits. "Show all" puts every section back on one page -- which is also what makes the browser's own
// find-in-page reach a field in a section that is not open.
(function () {
    'use strict';
    function storeKey(nav) { return 'pcaiAdminSec:' + (nav.dataset.subnav || ''); }
    function host(nav) { return nav.parentElement; }
    function sections(nav) {
        return Array.prototype.filter.call(host(nav).children, function (el) { return el.classList.contains('admin-sec'); });
    }

    function show(nav, sec, remember) {
        var secs = sections(nav);
        if (sec !== 'all' && !secs.some(function (s) { return s.dataset.sec === sec; })) sec = secs.length ? secs[0].dataset.sec : 'all';
        var h = host(nav);
        h.classList.add('subnav-on');
        h.classList.toggle('subnav-all', sec === 'all');
        secs.forEach(function (s) { s.classList.toggle('active', sec === 'all' || s.dataset.sec === sec); });
        nav.querySelectorAll('.subnav-btn').forEach(function (b) {
            var on = b.dataset.sec === sec;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        if (remember) { try { localStorage.setItem(storeKey(nav), sec); } catch (_) { /* private mode */ } }
        return sec;
    }

    function fromHash(tab) {
        var m = (location.hash || '').match(/^#tab-([a-z0-9_-]+)\/([a-z0-9_-]+)$/i);
        return m && m[1] === tab ? m[2] : '';
    }

    function init() {
        document.querySelectorAll('.admin-subnav').forEach(function (nav) {
            if (nav.dataset.ready) return;
            nav.dataset.ready = '1';
            var want = fromHash(nav.dataset.subnav);
            if (!want) { try { want = localStorage.getItem(storeKey(nav)) || ''; } catch (_) { } }
            show(nav, want, false);
            nav.addEventListener('click', function (e) {
                var b = e.target.closest('.subnav-btn');
                if (!b || !nav.contains(b)) return;
                e.preventDefault();
                show(nav, b.dataset.sec, true);
            });
        });
    }

    // A link to another section of the same tab ("Storage caps are under Retention").
    document.addEventListener('click', function (e) {
        var a = e.target.closest && e.target.closest('[data-sec-link]');
        if (!a) return;
        var pane = a.closest('.tab-content'), nav = pane && pane.querySelector(':scope > .admin-subnav');
        if (!nav) return;
        e.preventDefault();
        show(nav, a.dataset.secLink, true);
        try { nav.scrollIntoView({ block: 'start' }); } catch (_) { }
    });

    // For other scripts (and checks): open a section by name. Returns the section actually shown.
    window.pcAdminSection = function (tab, sec) {
        var nav = document.querySelector('.admin-subnav[data-subnav="' + tab + '"]');
        return nav ? show(nav, sec, true) : null;
    };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
