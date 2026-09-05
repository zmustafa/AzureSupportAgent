/* Progressive enhancement: all workflows remain readable without JavaScript. */
(() => {
  'use strict';
  const root = document.querySelector('[data-home-showcase]');
  if (!root) return;
  const navigation = root.querySelector('[data-showcase-navigation]');
  const tabs = Array.from(root.querySelectorAll('[data-showcase-tab]'));
  const panels = tabs.map(tab => document.getElementById(tab.hash.slice(1)));
  if (!navigation || tabs.length !== 4 || panels.some(panel => !panel)) return;
  const desktop = window.matchMedia('(min-width: 50rem)');
  let selected = Math.max(0, panels.findIndex(panel => `#${panel.id}` === window.location.hash));

  function select(index) {
    selected = index;
    tabs.forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(i === selected));
      tab.tabIndex = i === selected ? 0 : -1;
      panels[i].hidden = i !== selected;
    });
  }

  function layout() {
    root.classList.toggle('is-tabbed', desktop.matches);
    if (desktop.matches) {
      // Preserve focus when a resize would otherwise hide its current panel.
      const focused = panels.findIndex(panel => panel.contains(document.activeElement));
      if (focused >= 0) selected = focused;
      navigation.setAttribute('role', 'tablist');
      tabs.forEach((tab, i) => {
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', panels[i].id);
        panels[i].setAttribute('role', 'tabpanel');
        panels[i].setAttribute('aria-labelledby', tab.id);
        panels[i].tabIndex = 0;
      });
      select(selected);
    } else {
      navigation.removeAttribute('role');
      tabs.forEach((tab, i) => {
        ['role', 'aria-controls', 'aria-selected', 'tabindex'].forEach(attr => tab.removeAttribute(attr));
        panels[i].hidden = false;
        panels[i].removeAttribute('role');
        panels[i].removeAttribute('tabindex');
        panels[i].setAttribute('aria-labelledby', `${panels[i].id}-title`);
      });
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', event => {
      if (!desktop.matches || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      select(index);
    });
    tab.addEventListener('keydown', event => {
      if (!desktop.matches || event.ctrlKey || event.metaKey || event.altKey) return;
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        tabs.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
        tabs[next].focus();
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        select(index);
      }
    });
  });
  window.addEventListener('hashchange', () => {
    const index = panels.findIndex(panel => `#${panel.id}` === window.location.hash);
    if (index < 0) return;
    selected = index;
    if (desktop.matches) {
      select(index);
      panels[index].scrollIntoView({ block: 'start' });
    }
  });
  desktop.addEventListener('change', layout);
  layout();
})();