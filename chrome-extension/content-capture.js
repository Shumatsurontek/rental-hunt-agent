"use strict";

const BODY_TEXT_CHARS_MAX = 500_000;
const DESCRIPTION_CHARS_MAX = 20_000;
const DOCUMENT_HTML_PREFIX_CHARS_MAX = 100_000;
const JSON_DOCUMENTS_BYTES_MAX = 5_000_000;
const JSON_SCRIPT_BYTES_MAX = 2_000_000;
const JSON_SCRIPTS_MAX = 100;
const LISTINGS_PER_PAGE_MAX = 150;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "capture-seloger-page") return false;
  try {
    sendResponse({ page: captureSeLogerPage() });
  } catch (error) {
    sendResponse({ error: errorMessage(error) });
  }
  return false;
});

function captureSeLogerPage() {
  const bodyText = document.body?.innerText || "";
  if (bodyText.length > BODY_TEXT_CHARS_MAX) {
    throw new Error(`SeLoger body text exceeds ${BODY_TEXT_CHARS_MAX} characters.`);
  }
  const jsonDocuments = captureJsonDocuments();
  const anchors = Array.from(
    document.querySelectorAll("a[href*='/annonces/'], a[href*='/bien/'], a[href*='/location/']"),
  );
  const domCandidates = anchors.slice(0, LISTINGS_PER_PAGE_MAX).map(captureDomCandidate);
  return {
    url: window.location.href,
    body_text: bodyText,
    document_html_prefix: document.documentElement.outerHTML.slice(
      0,
      DOCUMENT_HTML_PREFIX_CHARS_MAX,
    ),
    json_documents: jsonDocuments,
    dom_candidate_count: anchors.length,
    dom_candidates: domCandidates,
    next_url: findNextUrl(),
  };
}

function captureJsonDocuments() {
  const scripts = Array.from(
    document.querySelectorAll("script[type='application/ld+json'], script#__NEXT_DATA__"),
  );
  if (scripts.length > JSON_SCRIPTS_MAX) {
    throw new Error(`SeLoger page contains more than ${JSON_SCRIPTS_MAX} JSON scripts.`);
  }
  let totalBytes = 0;
  return scripts.map((script) => {
    const value = script.textContent || "";
    const bytes = new TextEncoder().encode(value).byteLength;
    if (bytes > JSON_SCRIPT_BYTES_MAX) {
      throw new Error(`One SeLoger JSON script exceeds ${JSON_SCRIPT_BYTES_MAX} bytes.`);
    }
    totalBytes += bytes;
    if (totalBytes > JSON_DOCUMENTS_BYTES_MAX) {
      throw new Error(`SeLoger JSON scripts exceed ${JSON_DOCUMENTS_BYTES_MAX} bytes.`);
    }
    return value;
  });
}

function captureDomCandidate(anchor) {
  const card =
    anchor.closest(
      "[data-testid^='classified-card-mfe-'], [data-testid='serp-core-classified-card-testid'], article, li, [class*='card']",
    ) || anchor;
  const image = card.querySelector("img");
  return {
    href: anchor.href,
    title:
      anchor.getAttribute("title") ||
      card.querySelector("h2, h3")?.textContent ||
      anchor.textContent ||
      "",
    description: (card.innerText || "").slice(0, DESCRIPTION_CHARS_MAX + 1),
    image: image?.src || null,
  };
}

function findNextUrl() {
  const direct = document.querySelector("a[rel='next']");
  if (direct?.href) return direct.href;
  const next = Array.from(document.querySelectorAll("a[href]")).find((link) =>
    /suivant/i.test(link.textContent || ""),
  );
  return next?.href || null;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
