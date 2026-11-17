// frontend/review-reader.ts
var e = [
	"pdf",
	"text",
	"document",
	"images",
	"image"
];
function t(t) {
	return e.find((e) => e === t) ?? "none";
}
function n(e) {
	let n = e.dataset;
	return {
		key: n.readerSource ?? "",
		kind: t(n.kind),
		format: n.format ?? "",
		filename: n.filename ?? "",
		size: n.size ?? "",
		baseUrl: n.baseUrl ?? "",
		downloadUrl: n.downloadUrl ?? "",
		bookUrl: n.bookUrl ?? "",
		hint: n.hint ?? ""
	};
}
function r(e, t) {
	return `${e.baseUrl}text/?page=${t}`;
}
function i(e, t) {
	return `${e.baseUrl}document/?part=${t}`;
}
function a(e, t) {
	return `${e.baseUrl}pages/${t}/`;
}
function o(e) {
	return `${e.baseUrl}file/`;
}
function s(e) {
	let t = e.format ? e.format.toUpperCase() : "this file";
	return `${e.hint || `There is no in-browser preview for ${t}.`} Download the file to check it locally${e.bookUrl ? ", or request a conversion from the book page" : ""}.`;
}
async function c(e) {
	let t = await fetch(e, {
		credentials: "same-origin",
		headers: { Accept: "application/json" }
	}), n = null;
	try {
		n = await t.json();
	} catch {
		n = null;
	}
	if (!t.ok) {
		let e = n && typeof n == "object" && "error" in n ? String(n.error) : `The preview could not be loaded (HTTP ${t.status}).`;
		throw Error(e);
	}
	if (n === null) throw Error("The preview response was not readable.");
	return n;
}
async function l(e, t, n) {
	return (await import("./review-reader-pdf-p0PKNrl7.js")).renderPdf(e, t, n);
}
function u(e) {
	return typeof e == "object" && !!e && typeof e.text == "string" && typeof e.page == "number" && typeof e.page_count == "number";
}
function d(e) {
	return typeof e == "object" && !!e && Array.isArray(e.parts);
}
function f(e, t, n) {
	let r = document.createElement(e);
	return t && (r.className = t), n !== void 0 && (r.textContent = n), r;
}
var p = class {
	root;
	toggleButton;
	body;
	viewer;
	meta;
	tabs;
	deps;
	generation = 0;
	pdfHandle = null;
	activeKey = null;
	constructor(e, t = {}) {
		this.root = e, this.deps = {
			fetchJson: t.fetchJson ?? c,
			renderPdf: t.renderPdf ?? l
		}, this.toggleButton = e.querySelector("[data-reader-toggle]"), this.body = e.querySelector("[data-reader-body]"), this.viewer = e.querySelector("[data-reader-viewer]"), this.meta = e.querySelector("[data-reader-meta]"), this.tabs = Array.from(e.querySelectorAll("[data-reader-source]")), this.toggleButton?.addEventListener("click", () => this.toggle()), e.querySelector("[data-reader-close]")?.addEventListener("click", () => this.close()), e.addEventListener("keydown", (e) => {
			e.key === "Escape" && this.isOpen && this.close();
		});
		for (let e of this.tabs) e.addEventListener("click", () => {
			this.select(e.dataset.readerSource ?? "");
		});
	}
	get isOpen() {
		return this.root.classList.contains("is-open");
	}
	open() {
		this.root.classList.add("is-open"), this.body && (this.body.hidden = !1), this.toggleButton?.setAttribute("aria-expanded", "true"), this.activeKey === null && this.tabs.length > 0 && this.select(this.tabs[0]?.dataset.readerSource ?? ""), this.root.querySelector("[data-reader-close]")?.focus();
	}
	close() {
		this.root.classList.remove("is-open"), this.body && (this.body.hidden = !0), this.toggleButton?.setAttribute("aria-expanded", "false"), this.toggleButton?.focus();
	}
	toggle() {
		this.isOpen ? this.close() : this.open();
	}
	async select(e) {
		let t = this.tabs.find((t) => t.dataset.readerSource === e);
		if (!t || !this.viewer) return;
		let r = n(t);
		this.generation += 1;
		let i = this.generation;
		this.activeKey = e;
		for (let e of this.tabs) e.setAttribute("aria-selected", e === t ? "true" : "false");
		this.pdfHandle?.destroy(), this.pdfHandle = null, this.viewer.classList.remove("has-frame"), this.viewer.replaceChildren(f("p", "review-reader-message muted", "Loading…")), this.renderMeta(r);
		let a = () => i === this.generation;
		try {
			await this.renderSource(r, a);
		} catch (e) {
			if (a()) {
				let t = e instanceof Error ? e.message : String(e);
				this.showMessage(r, `The preview could not be shown: ${t}`);
			}
		}
	}
	renderMeta(e) {
		if (!this.meta) return;
		let t = [
			e.filename,
			e.format.toUpperCase(),
			e.size
		].filter(Boolean), n = f("a", void 0, "Download");
		n.href = e.downloadUrl, this.meta.replaceChildren(document.createTextNode(`${t.join(" · ")} · `), n);
	}
	showMessage(e, t) {
		if (!this.viewer) return;
		let n = f("div", "review-reader-message");
		n.append(f("p", void 0, t));
		let r = f("a", "button", "Download file");
		if (r.href = e.downloadUrl, n.append(r), e.bookUrl) {
			let t = f("a", void 0, "Open the book page (conversions)");
			t.href = e.bookUrl, n.append(t);
		}
		this.viewer.classList.remove("has-frame"), this.viewer.replaceChildren(n);
	}
	async renderSource(e, t) {
		let n = this.viewer;
		if (n) switch (e.kind) {
			case "text":
				await this.renderText(e, n, t);
				return;
			case "document":
				await this.renderDocument(e, n, t);
				return;
			case "images":
				await this.renderImages(e, n, t);
				return;
			case "image": {
				let t = f("div", "review-reader-pages"), r = f("img");
				r.src = o(e), r.alt = e.filename, t.append(r), n.replaceChildren(t);
				return;
			}
			case "pdf": {
				let r = f("div");
				n.replaceChildren(r);
				let i = this.root.dataset.pdfWorker ?? "", a = await this.deps.renderPdf(n, o(e), i);
				if (!t()) {
					a.destroy();
					return;
				}
				r.remove(), this.pdfHandle = a, this.meta && this.meta.prepend(document.createTextNode(`${a.pageCount} pages · `));
				return;
			}
			default: this.showMessage(e, s(e));
		}
	}
	async renderText(e, t, n) {
		let i = f("pre"), a = f("button", "secondary review-reader-more", "Load more");
		a.type = "button";
		let o = f("p", "review-reader-message muted"), s = 1, c = async () => {
			let t = await this.deps.fetchJson(r(e, s));
			if (n()) {
				if (!u(t)) throw Error("unexpected text response");
				i.append(document.createTextNode(t.text)), s = t.page + 1, a.hidden = t.page >= t.page_count, o.textContent = t.truncated && t.page >= t.page_count ? "The preview stops here; download the file for the complete text." : "";
			}
		};
		a.addEventListener("click", () => {
			a.disabled = !0, c().catch((e) => {
				o.textContent = e instanceof Error ? e.message : String(e);
			}).finally(() => {
				a.disabled = !1;
			});
		}), await c(), n() && t.replaceChildren(i, a, o);
	}
	async fetchParts(e) {
		let t = await this.deps.fetchJson(`${e.baseUrl}parts/`);
		if (!d(t) || t.parts.length === 0) throw Error("the file has no readable parts");
		return t;
	}
	async renderDocument(e, t, n) {
		let { parts: r } = await this.fetchParts(e);
		if (!n()) return;
		let a = f("iframe");
		a.setAttribute("sandbox", ""), a.setAttribute("referrerpolicy", "no-referrer"), a.title = `Preview of ${e.filename}`;
		let o = 1, s = f("select");
		s.setAttribute("aria-label", "Chapter");
		for (let e of r) {
			let t = f("option", void 0, `${e.index}. ${e.label}`);
			t.value = String(e.index), s.append(t);
		}
		let c = f("button", "secondary small-button", "Previous"), l = f("button", "secondary small-button", "Next");
		c.type = "button", l.type = "button";
		let u = (t) => {
			o = Math.min(Math.max(t, 1), r.length), s.value = String(o), c.disabled = o <= 1, l.disabled = o >= r.length, a.src = i(e, o);
		};
		if (s.addEventListener("change", () => u(Number(s.value))), c.addEventListener("click", () => u(o - 1)), l.addEventListener("click", () => u(o + 1)), u(1), t.classList.add("has-frame"), r.length > 1) {
			let e = f("div", "review-reader-nav");
			e.append(c, s, l), t.replaceChildren(e, a);
		} else t.replaceChildren(a);
	}
	async renderImages(e, t, n) {
		let { parts: r } = await this.fetchParts(e);
		if (!n()) return;
		let i = f("div", "review-reader-pages");
		for (let t of r) {
			let n = f("img");
			n.loading = "lazy", n.decoding = "async", n.alt = `Page ${t.index}: ${t.label}`, n.src = a(e, t.index), i.append(n);
		}
		t.replaceChildren(i);
	}
};
function m(e = document, t = {}) {
	return Array.from(e.querySelectorAll("[data-review-reader]")).filter((e) => e.dataset.readerReady !== "true").map((e) => (e.dataset.readerReady = "true", new p(e, t)));
}
m();
export { p as ReviewReader, i as documentPartUrl, o as fileUrl, m as initReviewReaders, a as pageImageUrl, t as parsePreviewKind, n as sourceFromElement, r as textPageUrl, s as unsupportedMessage };

//# sourceMappingURL=review-reader.js.map