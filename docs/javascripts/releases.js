// Point the Download buttons on the home page at the current GitHub release's assets, and highlight the visitor's
// platform. Without JavaScript (or if the API call fails) the buttons keep their default href: the releases page.
(function () {
  "use strict";

  var REPO = "AKJ7/CANExplorer";

  // data-download key -> a regex matching that platform's asset file name
  var PATTERNS = {
    windows: /-windows-x86_64-setup\.exe$/,
    macos: /-macos-arm64\.dmg$/,
    appimage: /-linux-x86_64\.AppImage$/,
    deb: /-linux-x86_64\.deb$/,
  };

  function detectPlatform() {
    var platform = (
      (navigator.userAgentData && navigator.userAgentData.platform) ||
      navigator.platform ||
      ""
    ).toLowerCase();
    var ua = (navigator.userAgent || "").toLowerCase();
    if (platform.indexOf("win") !== -1) return "windows";
    if (platform.indexOf("mac") !== -1) return "macos";
    if (platform.indexOf("linux") !== -1 || ua.indexOf("linux") !== -1) return "appimage";
    return null;
  }

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    var buttons = document.querySelectorAll("[data-download]");
    if (!buttons.length) return; // Only the home page has them
    var note = document.getElementById("download-note");

    var platform = detectPlatform();
    buttons.forEach(function (button) {
      if (platform && button.getAttribute("data-download") === platform) {
        button.classList.add("md-button--primary");
      }
    });

    fetch("https://api.github.com/repos/" + REPO + "/releases/latest", {
      headers: { Accept: "application/vnd.github+json" },
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (release) {
        var assets = release.assets || [];
        buttons.forEach(function (button) {
          var pattern = PATTERNS[button.getAttribute("data-download")];
          var asset = assets.filter(function (a) { return pattern.test(a.name); })[0];
          if (asset) button.href = asset.browser_download_url;
        });
        if (note) {
          var tag = release.tag_name || release.name || "";
          note.innerHTML =
            "Latest release: <strong>" + tag + "</strong>. Other files (zip archives, checksums) are on the " +
            '<a href="https://github.com/' + REPO + '/releases/latest">release page</a> — ' +
            'see <a href="getting-started/installation/">installation</a> for Snap and building from source.';
        }
      })
      .catch(function () {
        if (note) {
          note.innerHTML =
            'Download the latest installers from the ' +
            '<a href="https://github.com/' + REPO + '/releases/latest">release page</a> — ' +
            'see <a href="getting-started/installation/">installation</a> for details.';
        }
      });
  });
})();
