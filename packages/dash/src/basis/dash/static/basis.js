/* basis's script for every Dash app, served from the app's origin (basis.dash.assets), after the app's own assets/.
   It adds to the objects Dash and dash-ag-grid read, and never replaces one, since an app's own script may have made
   it first, and every name it adds is basis's own. */
(function () {
  "use strict";

  var KEY = "mantine-color-scheme-value"; /* where Mantine's provider reads the scheme when a page loads */
  var clientside = (window.dash_clientside = window.dash_clientside || {});
  var basis = (clientside.basis = clientside.basis || {});

  /* The light and dark switch: flips the scheme on <html>, keeps the choice, and answers the switch's own label. */
  basis.scheme = function (clicks) {
    var root = document.documentElement;
    var shown = root.getAttribute("data-mantine-color-scheme") === "dark" ? "dark" : "light";
    if (clicks) {
      shown = shown === "dark" ? "light" : "dark";
      root.setAttribute("data-mantine-color-scheme", shown);
      try {
        window.localStorage.setItem(KEY, shown);
      } catch (refused) {
        /* a browser that keeps nothing still switches */
      }
    }
    return shown === "dark" ? "Switch to the light scheme" : "Switch to the dark scheme";
  };
})();
