// \0rolldown/runtime.js
var e = Object.create, t = Object.defineProperty, n = Object.getOwnPropertyDescriptor, r = Object.getOwnPropertyNames, i = Object.getPrototypeOf, a = Object.prototype.hasOwnProperty, o = (e, t) => () => (t || (e((t = { exports: {} }).exports, t, t.exports), e = null), t.exports), s = (e, i, o, s) => {
	if (i && typeof i == "object" || typeof i == "function") for (var c = r(i), l = 0, u = c.length, d; l < u; l++) d = c[l], !a.call(e, d) && d !== o && t(e, d, {
		get: ((e) => i[e]).bind(null, d),
		enumerable: !(s = n(i, d)) || s.enumerable
	});
	return e;
}, c = /* @__PURE__ */ ((n, r, a) => (a = n == null ? {} : e(i(n)), s(t(a, "default", {
	value: n,
	enumerable: !0
}), n)))((/* @__PURE__ */ o(((e, t, n) => {
	(function(n, r) {
		typeof e == "object" && t !== void 0 ? t.exports = r() : typeof define == "function" && define.amd ? define(r) : (n = typeof globalThis < "u" ? globalThis : n || self).interact = r();
	})(n, (function() {
		function e(e, t) {
			var n = Object.keys(e);
			if (Object.getOwnPropertySymbols) {
				var r = Object.getOwnPropertySymbols(e);
				t && (r = r.filter((function(t) {
					return Object.getOwnPropertyDescriptor(e, t).enumerable;
				}))), n.push.apply(n, r);
			}
			return n;
		}
		function n(t) {
			for (var n = 1; n < arguments.length; n++) {
				var r = arguments[n] == null ? {} : arguments[n];
				n % 2 ? e(Object(r), !0).forEach((function(e) {
					s(t, e, r[e]);
				})) : Object.getOwnPropertyDescriptors ? Object.defineProperties(t, Object.getOwnPropertyDescriptors(r)) : e(Object(r)).forEach((function(e) {
					Object.defineProperty(t, e, Object.getOwnPropertyDescriptor(r, e));
				}));
			}
			return t;
		}
		function r(e) {
			return r = typeof Symbol == "function" && typeof Symbol.iterator == "symbol" ? function(e) {
				return typeof e;
			} : function(e) {
				return e && typeof Symbol == "function" && e.constructor === Symbol && e !== Symbol.prototype ? "symbol" : typeof e;
			}, r(e);
		}
		function i(e, t) {
			if (!(e instanceof t)) throw TypeError("Cannot call a class as a function");
		}
		function a(e, t) {
			for (var n = 0; n < t.length; n++) {
				var r = t[n];
				r.enumerable = r.enumerable || !1, r.configurable = !0, "value" in r && (r.writable = !0), Object.defineProperty(e, m(r.key), r);
			}
		}
		function o(e, t, n) {
			return t && a(e.prototype, t), n && a(e, n), Object.defineProperty(e, "prototype", { writable: !1 }), e;
		}
		function s(e, t, n) {
			return (t = m(t)) in e ? Object.defineProperty(e, t, {
				value: n,
				enumerable: !0,
				configurable: !0,
				writable: !0
			}) : e[t] = n, e;
		}
		function c(e, t) {
			if (typeof t != "function" && t !== null) throw TypeError("Super expression must either be null or a function");
			e.prototype = Object.create(t && t.prototype, { constructor: {
				value: e,
				writable: !0,
				configurable: !0
			} }), Object.defineProperty(e, "prototype", { writable: !1 }), t && u(e, t);
		}
		function l(e) {
			return l = Object.setPrototypeOf ? Object.getPrototypeOf.bind() : function(e) {
				return e.__proto__ || Object.getPrototypeOf(e);
			}, l(e);
		}
		function u(e, t) {
			return u = Object.setPrototypeOf ? Object.setPrototypeOf.bind() : function(e, t) {
				return e.__proto__ = t, e;
			}, u(e, t);
		}
		function d(e) {
			if (e === void 0) throw ReferenceError("this hasn't been initialised - super() hasn't been called");
			return e;
		}
		function f(e) {
			var t = function() {
				if (typeof Reflect > "u" || !Reflect.construct || Reflect.construct.sham) return !1;
				if (typeof Proxy == "function") return !0;
				try {
					return Boolean.prototype.valueOf.call(Reflect.construct(Boolean, [], (function() {}))), !0;
				} catch {
					return !1;
				}
			}();
			return function() {
				var n, r = l(e);
				if (t) {
					var i = l(this).constructor;
					n = Reflect.construct(r, arguments, i);
				} else n = r.apply(this, arguments);
				return function(e, t) {
					if (t && (typeof t == "object" || typeof t == "function")) return t;
					if (t !== void 0) throw TypeError("Derived constructors may only return object or undefined");
					return d(e);
				}(this, n);
			};
		}
		function p() {
			return p = typeof Reflect < "u" && Reflect.get ? Reflect.get.bind() : function(e, t, n) {
				var r = function(e, t) {
					for (; !Object.prototype.hasOwnProperty.call(e, t) && (e = l(e)) !== null;);
					return e;
				}(e, t);
				if (r) {
					var i = Object.getOwnPropertyDescriptor(r, t);
					return i.get ? i.get.call(arguments.length < 3 ? e : n) : i.value;
				}
			}, p.apply(this, arguments);
		}
		function m(e) {
			var t = function(e, t) {
				if (typeof e != "object" || !e) return e;
				var n = e[Symbol.toPrimitive];
				if (n !== void 0) {
					var r = n.call(e, t);
					if (typeof r != "object") return r;
					throw TypeError("@@toPrimitive must return a primitive value.");
				}
				return String(e);
			}(e, "string");
			return typeof t == "symbol" ? t : t + "";
		}
		var h = function(e) {
			return !(!e || !e.Window) && e instanceof e.Window;
		}, g = void 0, _ = void 0;
		function v(e) {
			g = e;
			var t = e.document.createTextNode("");
			t.ownerDocument !== e.document && typeof e.wrap == "function" && e.wrap(t) === t && (e = e.wrap(e)), _ = e;
		}
		function y(e) {
			return h(e) ? e : (e.ownerDocument || e).defaultView || _.window;
		}
		typeof window < "u" && window && v(window);
		var b = function(e) {
			return !!e && r(e) === "object";
		}, x = function(e) {
			return typeof e == "function";
		}, S = {
			window: function(e) {
				return e === _ || h(e);
			},
			docFrag: function(e) {
				return b(e) && e.nodeType === 11;
			},
			object: b,
			func: x,
			number: function(e) {
				return typeof e == "number";
			},
			bool: function(e) {
				return typeof e == "boolean";
			},
			string: function(e) {
				return typeof e == "string";
			},
			element: function(e) {
				if (!e || r(e) !== "object") return !1;
				var t = y(e) || _;
				return /object|function/.test(typeof Element > "u" ? "undefined" : r(Element)) ? e instanceof Element || e instanceof t.Element : e.nodeType === 1 && typeof e.nodeName == "string";
			},
			plainObject: function(e) {
				return b(e) && !!e.constructor && /function Object\b/.test(e.constructor.toString());
			},
			array: function(e) {
				return b(e) && e.length !== void 0 && x(e.splice);
			}
		};
		function ee(e) {
			var t = e.interaction;
			if (t.prepared.name === "drag") {
				var n = t.prepared.axis;
				n === "x" ? (t.coords.cur.page.y = t.coords.start.page.y, t.coords.cur.client.y = t.coords.start.client.y, t.coords.velocity.client.y = 0, t.coords.velocity.page.y = 0) : n === "y" && (t.coords.cur.page.x = t.coords.start.page.x, t.coords.cur.client.x = t.coords.start.client.x, t.coords.velocity.client.x = 0, t.coords.velocity.page.x = 0);
			}
		}
		function C(e) {
			var t = e.iEvent, n = e.interaction;
			if (n.prepared.name === "drag") {
				var r = n.prepared.axis;
				if (r === "x" || r === "y") {
					var i = r === "x" ? "y" : "x";
					t.page[i] = n.coords.start.page[i], t.client[i] = n.coords.start.client[i], t.delta[i] = 0;
				}
			}
		}
		var w = {
			id: "actions/drag",
			install: function(e) {
				var t = e.actions, n = e.Interactable, r = e.defaults;
				n.prototype.draggable = w.draggable, t.map.drag = w, t.methodDict.drag = "draggable", r.actions.drag = w.defaults;
			},
			listeners: {
				"interactions:before-action-move": ee,
				"interactions:action-resume": ee,
				"interactions:action-move": C,
				"auto-start:check": function(e) {
					var t = e.interaction, n = e.interactable, r = e.buttons, i = n.options.drag;
					if (i && i.enabled && (!t.pointerIsDown || !/mouse|pointer/.test(t.pointerType) || (r & n.options.drag.mouseButtons) != 0)) return e.action = {
						name: "drag",
						axis: i.lockAxis === "start" ? i.startAxis : i.lockAxis
					}, !1;
				}
			},
			draggable: function(e) {
				return S.object(e) ? (this.options.drag.enabled = !1 !== e.enabled, this.setPerAction("drag", e), this.setOnEvents("drag", e), /^(xy|x|y|start)$/.test(e.lockAxis) && (this.options.drag.lockAxis = e.lockAxis), /^(xy|x|y)$/.test(e.startAxis) && (this.options.drag.startAxis = e.startAxis), this) : S.bool(e) ? (this.options.drag.enabled = e, this) : this.options.drag;
			},
			beforeMove: ee,
			move: C,
			defaults: {
				startAxis: "xy",
				lockAxis: "xy"
			},
			getCursor: function() {
				return "move";
			},
			filterEventType: function(e) {
				return e.search("drag") === 0;
			}
		}, T = w, E = {
			init: function(e) {
				var t = e;
				E.document = t.document, E.DocumentFragment = t.DocumentFragment || te, E.SVGElement = t.SVGElement || te, E.SVGSVGElement = t.SVGSVGElement || te, E.SVGElementInstance = t.SVGElementInstance || te, E.Element = t.Element || te, E.HTMLElement = t.HTMLElement || E.Element, E.Event = t.Event, E.Touch = t.Touch || te, E.PointerEvent = t.PointerEvent || t.MSPointerEvent;
			},
			document: null,
			DocumentFragment: null,
			SVGElement: null,
			SVGSVGElement: null,
			SVGElementInstance: null,
			Element: null,
			HTMLElement: null,
			Event: null,
			Touch: null,
			PointerEvent: null
		};
		function te() {}
		var D = E, O = {
			init: function(e) {
				var t = D.Element, n = e.navigator || {};
				O.supportsTouch = "ontouchstart" in e || S.func(e.DocumentTouch) && D.document instanceof e.DocumentTouch, O.supportsPointerEvent = !1 !== n.pointerEnabled && !!D.PointerEvent, O.isIOS = /iP(hone|od|ad)/.test(n.platform), O.isIOS7 = /iP(hone|od|ad)/.test(n.platform) && /OS 7[^\d]/.test(n.appVersion), O.isIe9 = /MSIE 9/.test(n.userAgent), O.isOperaMobile = n.appName === "Opera" && O.supportsTouch && /Presto/.test(n.userAgent), O.prefixedMatchesSelector = "matches" in t.prototype ? "matches" : "webkitMatchesSelector" in t.prototype ? "webkitMatchesSelector" : "mozMatchesSelector" in t.prototype ? "mozMatchesSelector" : "oMatchesSelector" in t.prototype ? "oMatchesSelector" : "msMatchesSelector", O.pEventTypes = O.supportsPointerEvent ? D.PointerEvent === e.MSPointerEvent ? {
					up: "MSPointerUp",
					down: "MSPointerDown",
					over: "mouseover",
					out: "mouseout",
					move: "MSPointerMove",
					cancel: "MSPointerCancel"
				} : {
					up: "pointerup",
					down: "pointerdown",
					over: "pointerover",
					out: "pointerout",
					move: "pointermove",
					cancel: "pointercancel"
				} : null, O.wheelEvent = D.document && "onmousewheel" in D.document ? "mousewheel" : "wheel";
			},
			supportsTouch: null,
			supportsPointerEvent: null,
			isIOS7: null,
			isIOS: null,
			isIe9: null,
			isOperaMobile: null,
			prefixedMatchesSelector: null,
			pEventTypes: null,
			wheelEvent: null
		}, k = O;
		function ne(e, t) {
			if (e.contains) return e.contains(t);
			for (; t;) {
				if (t === e) return !0;
				t = t.parentNode;
			}
			return !1;
		}
		function re(e, t) {
			for (; S.element(e);) {
				if (j(e, t)) return e;
				e = A(e);
			}
			return null;
		}
		function A(e) {
			var t = e.parentNode;
			if (S.docFrag(t)) {
				for (; (t = t.host) && S.docFrag(t););
				return t;
			}
			return t;
		}
		function j(e, t) {
			return _ !== g && (t = t.replace(/\/deep\//g, " ")), e[k.prefixedMatchesSelector](t);
		}
		var ie = function(e) {
			return e.parentNode || e.host;
		};
		function ae(e, t) {
			for (var n, r = [], i = e; (n = ie(i)) && i !== t && n !== i.ownerDocument;) r.unshift(i), i = n;
			return r;
		}
		function oe(e, t, n) {
			for (; S.element(e);) {
				if (j(e, t)) return !0;
				if ((e = A(e)) === n) return j(e, t);
			}
			return !1;
		}
		function se(e) {
			return e.correspondingUseElement || e;
		}
		function ce(e) {
			var t = e instanceof D.SVGElement ? e.getBoundingClientRect() : e.getClientRects()[0];
			return t && {
				left: t.left,
				right: t.right,
				top: t.top,
				bottom: t.bottom,
				width: t.width || t.right - t.left,
				height: t.height || t.bottom - t.top
			};
		}
		function le(e) {
			var t, n = ce(e);
			if (!k.isIOS7 && n) {
				var r = {
					x: (t = (t = y(e)) || _).scrollX || t.document.documentElement.scrollLeft,
					y: t.scrollY || t.document.documentElement.scrollTop
				};
				n.left += r.x, n.right += r.x, n.top += r.y, n.bottom += r.y;
			}
			return n;
		}
		function M(e) {
			for (var t = []; e;) t.push(e), e = A(e);
			return t;
		}
		function ue(e) {
			return !!S.string(e) && (D.document.querySelector(e), !0);
		}
		function N(e, t) {
			for (var n in t) e[n] = t[n];
			return e;
		}
		function de(e, t, n) {
			return e === "parent" ? A(n) : e === "self" ? t.getRect(n) : re(n, e);
		}
		function fe(e, t, n, r) {
			var i = e;
			return S.string(i) ? i = de(i, t, n) : S.func(i) && (i = i.apply(void 0, r)), S.element(i) && (i = le(i)), i;
		}
		function pe(e) {
			return e && {
				x: "x" in e ? e.x : e.left,
				y: "y" in e ? e.y : e.top
			};
		}
		function me(e) {
			return !e || "x" in e && "y" in e || ((e = N({}, e)).x = e.left || 0, e.y = e.top || 0, e.width = e.width || (e.right || 0) - e.x, e.height = e.height || (e.bottom || 0) - e.y), e;
		}
		function P(e, t, n) {
			e.left && (t.left += n.x), e.right && (t.right += n.x), e.top && (t.top += n.y), e.bottom && (t.bottom += n.y), t.width = t.right - t.left, t.height = t.bottom - t.top;
		}
		function he(e, t, n) {
			var r = n && e.options[n];
			return pe(fe(r && r.origin || e.options.origin, e, t, [e && t])) || {
				x: 0,
				y: 0
			};
		}
		function ge(e, t) {
			var n = arguments.length > 2 && arguments[2] !== void 0 ? arguments[2] : function(e) {
				return !0;
			}, r = arguments.length > 3 ? arguments[3] : void 0;
			if (r ||= {}, S.string(e) && e.search(" ") !== -1 && (e = _e(e)), S.array(e)) return e.forEach((function(e) {
				return ge(e, t, n, r);
			})), r;
			if (S.object(e) && (t = e, e = ""), S.func(t) && n(e)) r[e] = r[e] || [], r[e].push(t);
			else if (S.array(t)) for (var i = 0, a = t; i < a.length; i++) {
				var o = a[i];
				ge(e, o, n, r);
			}
			else if (S.object(t)) for (var s in t) ge(_e(s).map((function(t) {
				return `${e}${t}`;
			})), t[s], n, r);
			return r;
		}
		function _e(e) {
			return e.trim().split(/ +/);
		}
		var ve = function(e, t) {
			return Math.sqrt(e * e + t * t);
		}, ye = ["webkit", "moz"];
		function be(e, t) {
			e.__set ||= {};
			var n = function(n) {
				if (ye.some((function(e) {
					return n.indexOf(e) === 0;
				}))) return 1;
				typeof e[n] != "function" && n !== "__set" && Object.defineProperty(e, n, {
					get: function() {
						return n in e.__set ? e.__set[n] : e.__set[n] = t[n];
					},
					set: function(t) {
						e.__set[n] = t;
					},
					configurable: !0
				});
			};
			for (var r in t) n(r);
			return e;
		}
		function xe(e, t) {
			e.page = e.page || {}, e.page.x = t.page.x, e.page.y = t.page.y, e.client = e.client || {}, e.client.x = t.client.x, e.client.y = t.client.y, e.timeStamp = t.timeStamp;
		}
		function Se(e) {
			e.page.x = 0, e.page.y = 0, e.client.x = 0, e.client.y = 0;
		}
		function F(e) {
			return e instanceof D.Event || e instanceof D.Touch;
		}
		function Ce(e, t, n) {
			return e ||= "page", (n ||= {}).x = t[e + "X"], n.y = t[e + "Y"], n;
		}
		function we(e, t) {
			return t ||= {
				x: 0,
				y: 0
			}, k.isOperaMobile && F(e) ? (Ce("screen", e, t), t.x += window.scrollX, t.y += window.scrollY) : Ce("page", e, t), t;
		}
		function I(e) {
			return S.number(e.pointerId) ? e.pointerId : e.identifier;
		}
		function Te(e, t, n) {
			var r = t.length > 1 ? Ee(t) : t[0];
			we(r, e.page), function(e, t) {
				t ||= {}, k.isOperaMobile && F(e) ? Ce("screen", e, t) : Ce("client", e, t);
			}(r, e.client), e.timeStamp = n;
		}
		function L(e) {
			var t = [];
			return S.array(e) ? (t[0] = e[0], t[1] = e[1]) : e.type === "touchend" ? e.touches.length === 1 ? (t[0] = e.touches[0], t[1] = e.changedTouches[0]) : e.touches.length === 0 && (t[0] = e.changedTouches[0], t[1] = e.changedTouches[1]) : (t[0] = e.touches[0], t[1] = e.touches[1]), t;
		}
		function Ee(e) {
			for (var t = {
				pageX: 0,
				pageY: 0,
				clientX: 0,
				clientY: 0,
				screenX: 0,
				screenY: 0
			}, n = 0; n < e.length; n++) {
				var r = e[n];
				for (var i in t) t[i] += r[i];
			}
			for (var a in t) t[a] /= e.length;
			return t;
		}
		function De(e) {
			if (!e.length) return null;
			var t = L(e), n = Math.min(t[0].pageX, t[1].pageX), r = Math.min(t[0].pageY, t[1].pageY), i = Math.max(t[0].pageX, t[1].pageX), a = Math.max(t[0].pageY, t[1].pageY);
			return {
				x: n,
				y: r,
				left: n,
				top: r,
				right: i,
				bottom: a,
				width: i - n,
				height: a - r
			};
		}
		function Oe(e, t) {
			var n = t + "X", r = t + "Y", i = L(e);
			return ve(i[0][n] - i[1][n], i[0][r] - i[1][r]);
		}
		function ke(e, t) {
			var n = t + "X", r = t + "Y", i = L(e), a = i[1][n] - i[0][n], o = i[1][r] - i[0][r];
			return 180 * Math.atan2(o, a) / Math.PI;
		}
		function Ae(e) {
			return S.string(e.pointerType) ? e.pointerType : S.number(e.pointerType) ? [
				void 0,
				void 0,
				"touch",
				"pen",
				"mouse"
			][e.pointerType] : /touch/.test(e.type || "") || e instanceof D.Touch ? "touch" : "mouse";
		}
		function je(e) {
			var t = S.func(e.composedPath) ? e.composedPath() : e.path;
			return [se(t ? t[0] : e.target), se(e.currentTarget)];
		}
		var Me = function() {
			function e(t) {
				i(this, e), this.immediatePropagationStopped = !1, this.propagationStopped = !1, this._interaction = t;
			}
			return o(e, [
				{
					key: "preventDefault",
					value: function() {}
				},
				{
					key: "stopPropagation",
					value: function() {
						this.propagationStopped = !0;
					}
				},
				{
					key: "stopImmediatePropagation",
					value: function() {
						this.immediatePropagationStopped = this.propagationStopped = !0;
					}
				}
			]), e;
		}();
		Object.defineProperty(Me.prototype, "interaction", {
			get: function() {
				return this._interaction._proxy;
			},
			set: function() {}
		});
		var Ne = function(e, t) {
			for (var n = 0; n < t.length; n++) {
				var r = t[n];
				e.push(r);
			}
			return e;
		}, Pe = function(e) {
			return Ne([], e);
		}, Fe = function(e, t) {
			for (var n = 0; n < e.length; n++) if (t(e[n], n, e)) return n;
			return -1;
		}, Ie = function(e, t) {
			return e[Fe(e, t)];
		}, Le = function(e) {
			c(n, e);
			var t = f(n);
			function n(e, r, a) {
				var o;
				i(this, n), (o = t.call(this, r._interaction)).dropzone = void 0, o.dragEvent = void 0, o.relatedTarget = void 0, o.draggable = void 0, o.propagationStopped = !1, o.immediatePropagationStopped = !1;
				var s = a === "dragleave" ? e.prev : e.cur, c = s.element, l = s.dropzone;
				return o.type = a, o.target = c, o.currentTarget = c, o.dropzone = l, o.dragEvent = r, o.relatedTarget = r.target, o.draggable = r.interactable, o.timeStamp = r.timeStamp, o;
			}
			return o(n, [
				{
					key: "reject",
					value: function() {
						var e = this, t = this._interaction.dropState;
						if (this.type === "dropactivate" || this.dropzone && t.cur.dropzone === this.dropzone && t.cur.element === this.target) {
							if (t.prev.dropzone = this.dropzone, t.prev.element = this.target, t.rejected = !0, t.events.enter = null, this.stopImmediatePropagation(), this.type === "dropactivate") {
								var r = t.activeDrops, i = Fe(r, (function(t) {
									var n = t.dropzone, r = t.element;
									return n === e.dropzone && r === e.target;
								}));
								t.activeDrops.splice(i, 1);
								var a = new n(t, this.dragEvent, "dropdeactivate");
								a.dropzone = this.dropzone, a.target = this.target, this.dropzone.fire(a);
							} else this.dropzone.fire(new n(t, this.dragEvent, "dragleave"));
						}
					}
				},
				{
					key: "preventDefault",
					value: function() {}
				},
				{
					key: "stopPropagation",
					value: function() {
						this.propagationStopped = !0;
					}
				},
				{
					key: "stopImmediatePropagation",
					value: function() {
						this.immediatePropagationStopped = this.propagationStopped = !0;
					}
				}
			]), n;
		}(Me);
		function Re(e, t) {
			for (var n = 0, r = e.slice(); n < r.length; n++) {
				var i = r[n], a = i.dropzone, o = i.element;
				t.dropzone = a, t.target = o, a.fire(t), t.propagationStopped = t.immediatePropagationStopped = !1;
			}
		}
		function ze(e, t) {
			for (var n = function(e, t) {
				for (var n = [], r = 0, i = e.interactables.list; r < i.length; r++) {
					var a = i[r];
					if (a.options.drop.enabled) {
						var o = a.options.drop.accept;
						if (!(S.element(o) && o !== t || S.string(o) && !j(t, o) || S.func(o) && !o({
							dropzone: a,
							draggableElement: t
						}))) for (var s = 0, c = a.getAllElements(); s < c.length; s++) {
							var l = c[s];
							l !== t && n.push({
								dropzone: a,
								element: l,
								rect: a.getRect(l)
							});
						}
					}
				}
				return n;
			}(e, t), r = 0; r < n.length; r++) {
				var i = n[r];
				i.rect = i.dropzone.getRect(i.element);
			}
			return n;
		}
		function Be(e, t, n) {
			for (var r = e.dropState, i = e.interactable, a = e.element, o = [], s = 0, c = r.activeDrops; s < c.length; s++) {
				var l = c[s], u = l.dropzone, d = l.element, f = l.rect, p = u.dropCheck(t, n, i, a, d, f);
				o.push(p ? d : null);
			}
			var m = function(e) {
				for (var t, n, r, i = [], a = 0; a < e.length; a++) {
					var o = e[a], s = e[t];
					if (o && a !== t) {
						if (s) {
							var c = ie(o), l = ie(s);
							if (c !== o.ownerDocument) {
								if (l !== o.ownerDocument) {
									if (c !== l) {
										i = i.length ? i : ae(s);
										var u = void 0;
										if (s instanceof D.HTMLElement && o instanceof D.SVGElement && !(o instanceof D.SVGSVGElement)) {
											if (o === l) continue;
											u = o.ownerSVGElement;
										} else u = o;
										for (var d = ae(u, s.ownerDocument), f = 0; d[f] && d[f] === i[f];) f++;
										var p = [
											d[f - 1],
											d[f],
											i[f]
										];
										if (p[0]) for (var m = p[0].lastChild; m;) {
											if (m === p[1]) {
												t = a, i = d;
												break;
											}
											if (m === p[2]) break;
											m = m.previousSibling;
										}
									} else r = s, (parseInt(y(n = o).getComputedStyle(n).zIndex, 10) || 0) >= (parseInt(y(r).getComputedStyle(r).zIndex, 10) || 0) && (t = a);
								} else t = a;
							}
						} else t = a;
					}
				}
				return t;
			}(o);
			return r.activeDrops[m] || null;
		}
		function Ve(e, t, n) {
			var r = e.dropState, i = {
				enter: null,
				leave: null,
				activate: null,
				deactivate: null,
				move: null,
				drop: null
			};
			return n.type === "dragstart" && (i.activate = new Le(r, n, "dropactivate"), i.activate.target = null, i.activate.dropzone = null), n.type === "dragend" && (i.deactivate = new Le(r, n, "dropdeactivate"), i.deactivate.target = null, i.deactivate.dropzone = null), r.rejected || (r.cur.element !== r.prev.element && (r.prev.dropzone && (i.leave = new Le(r, n, "dragleave"), n.dragLeave = i.leave.target = r.prev.element, n.prevDropzone = i.leave.dropzone = r.prev.dropzone), r.cur.dropzone && (i.enter = new Le(r, n, "dragenter"), n.dragEnter = r.cur.element, n.dropzone = r.cur.dropzone)), n.type === "dragend" && r.cur.dropzone && (i.drop = new Le(r, n, "drop"), n.dropzone = r.cur.dropzone, n.relatedTarget = r.cur.element), n.type === "dragmove" && r.cur.dropzone && (i.move = new Le(r, n, "dropmove"), n.dropzone = r.cur.dropzone)), i;
		}
		function He(e, t) {
			var n = e.dropState, r = n.activeDrops, i = n.cur, a = n.prev;
			t.leave && a.dropzone.fire(t.leave), t.enter && i.dropzone.fire(t.enter), t.move && i.dropzone.fire(t.move), t.drop && i.dropzone.fire(t.drop), t.deactivate && Re(r, t.deactivate), n.prev.dropzone = i.dropzone, n.prev.element = i.element;
		}
		function R(e, t) {
			var n = e.interaction, r = e.iEvent, i = e.event;
			if (r.type === "dragmove" || r.type === "dragend") {
				var a = n.dropState;
				t.dynamicDrop && (a.activeDrops = ze(t, n.element));
				var o = r, s = Be(n, o, i);
				a.rejected = a.rejected && !!s && s.dropzone === a.cur.dropzone && s.element === a.cur.element, a.cur.dropzone = s && s.dropzone, a.cur.element = s && s.element, a.events = Ve(n, 0, o);
			}
		}
		var z = {
			id: "actions/drop",
			install: function(e) {
				var t = e.actions, n = e.interactStatic, r = e.Interactable, i = e.defaults;
				e.usePlugin(T), r.prototype.dropzone = function(e) {
					return function(e, t) {
						if (S.object(t)) {
							if (e.options.drop.enabled = !1 !== t.enabled, t.listeners) {
								var n = ge(t.listeners), r = Object.keys(n).reduce((function(e, t) {
									return e[/^(enter|leave)/.test(t) ? `drag${t}` : /^(activate|deactivate|move)/.test(t) ? `drop${t}` : t] = n[t], e;
								}), {}), i = e.options.drop.listeners;
								i && e.off(i), e.on(r), e.options.drop.listeners = r;
							}
							return S.func(t.ondrop) && e.on("drop", t.ondrop), S.func(t.ondropactivate) && e.on("dropactivate", t.ondropactivate), S.func(t.ondropdeactivate) && e.on("dropdeactivate", t.ondropdeactivate), S.func(t.ondragenter) && e.on("dragenter", t.ondragenter), S.func(t.ondragleave) && e.on("dragleave", t.ondragleave), S.func(t.ondropmove) && e.on("dropmove", t.ondropmove), /^(pointer|center)$/.test(t.overlap) ? e.options.drop.overlap = t.overlap : S.number(t.overlap) && (e.options.drop.overlap = Math.max(Math.min(1, t.overlap), 0)), "accept" in t && (e.options.drop.accept = t.accept), "checker" in t && (e.options.drop.checker = t.checker), e;
						}
						return S.bool(t) ? (e.options.drop.enabled = t, e) : e.options.drop;
					}(this, e);
				}, r.prototype.dropCheck = function(e, t, n, r, i, a) {
					return function(e, t, n, r, i, a, o) {
						var s = !1;
						if (!(o ||= e.getRect(a))) return !!e.options.drop.checker && e.options.drop.checker(t, n, s, e, a, r, i);
						var c = e.options.drop.overlap;
						if (c === "pointer") {
							var l = he(r, i, "drag"), u = we(t);
							u.x += l.x, u.y += l.y;
							var d = u.x > o.left && u.x < o.right, f = u.y > o.top && u.y < o.bottom;
							s = d && f;
						}
						var p = r.getRect(i);
						if (p && c === "center") {
							var m = p.left + p.width / 2, h = p.top + p.height / 2;
							s = m >= o.left && m <= o.right && h >= o.top && h <= o.bottom;
						}
						return p && S.number(c) && (s = Math.max(0, Math.min(o.right, p.right) - Math.max(o.left, p.left)) * Math.max(0, Math.min(o.bottom, p.bottom) - Math.max(o.top, p.top)) / (p.width * p.height) >= c), e.options.drop.checker && (s = e.options.drop.checker(t, n, s, e, a, r, i)), s;
					}(this, e, t, n, r, i, a);
				}, n.dynamicDrop = function(t) {
					return S.bool(t) ? (e.dynamicDrop = t, n) : e.dynamicDrop;
				}, N(t.phaselessTypes, {
					dragenter: !0,
					dragleave: !0,
					dropactivate: !0,
					dropdeactivate: !0,
					dropmove: !0,
					drop: !0
				}), t.methodDict.drop = "dropzone", e.dynamicDrop = !1, i.actions.drop = z.defaults;
			},
			listeners: {
				"interactions:before-action-start": function(e) {
					var t = e.interaction;
					t.prepared.name === "drag" && (t.dropState = {
						cur: {
							dropzone: null,
							element: null
						},
						prev: {
							dropzone: null,
							element: null
						},
						rejected: null,
						events: null,
						activeDrops: []
					});
				},
				"interactions:after-action-start": function(e, t) {
					var n = e.interaction, r = (e.event, e.iEvent);
					if (n.prepared.name === "drag") {
						var i = n.dropState;
						i.activeDrops = [], i.events = {}, i.activeDrops = ze(t, n.element), i.events = Ve(n, 0, r), i.events.activate && (Re(i.activeDrops, i.events.activate), t.fire("actions/drop:start", {
							interaction: n,
							dragEvent: r
						}));
					}
				},
				"interactions:action-move": R,
				"interactions:after-action-move": function(e, t) {
					var n = e.interaction, r = e.iEvent;
					if (n.prepared.name === "drag") {
						var i = n.dropState;
						He(n, i.events), t.fire("actions/drop:move", {
							interaction: n,
							dragEvent: r
						}), i.events = {};
					}
				},
				"interactions:action-end": function(e, t) {
					if (e.interaction.prepared.name === "drag") {
						var n = e.interaction, r = e.iEvent;
						R(e, t), He(n, n.dropState.events), t.fire("actions/drop:end", {
							interaction: n,
							dragEvent: r
						});
					}
				},
				"interactions:stop": function(e) {
					var t = e.interaction;
					if (t.prepared.name === "drag") {
						var n = t.dropState;
						n && (n.activeDrops = null, n.events = null, n.cur.dropzone = null, n.cur.element = null, n.prev.dropzone = null, n.prev.element = null, n.rejected = !1);
					}
				}
			},
			getActiveDrops: ze,
			getDrop: Be,
			getDropEvents: Ve,
			fireDropEvents: He,
			filterEventType: function(e) {
				return e.search("drag") === 0 || e.search("drop") === 0;
			},
			defaults: {
				enabled: !1,
				accept: null,
				overlap: "pointer"
			}
		}, Ue = z;
		function B(e) {
			var t = e.interaction, n = e.iEvent, r = e.phase;
			if (t.prepared.name === "gesture") {
				var i = t.pointers.map((function(e) {
					return e.pointer;
				})), a = r === "start", o = r === "end", s = t.interactable.options.deltaSource;
				if (n.touches = [i[0], i[1]], a) n.distance = Oe(i, s), n.box = De(i), n.scale = 1, n.ds = 0, n.angle = ke(i, s), n.da = 0, t.gesture.startDistance = n.distance, t.gesture.startAngle = n.angle;
				else if (o || t.pointers.length < 2) {
					var c = t.prevEvent;
					n.distance = c.distance, n.box = c.box, n.scale = c.scale, n.ds = 0, n.angle = c.angle, n.da = 0;
				} else n.distance = Oe(i, s), n.box = De(i), n.scale = n.distance / t.gesture.startDistance, n.angle = ke(i, s), n.ds = n.scale - t.gesture.scale, n.da = n.angle - t.gesture.angle;
				t.gesture.distance = n.distance, t.gesture.angle = n.angle, S.number(n.scale) && n.scale !== 1 / 0 && !isNaN(n.scale) && (t.gesture.scale = n.scale);
			}
		}
		var We = {
			id: "actions/gesture",
			before: ["actions/drag", "actions/resize"],
			install: function(e) {
				var t = e.actions, n = e.Interactable, r = e.defaults;
				n.prototype.gesturable = function(e) {
					return S.object(e) ? (this.options.gesture.enabled = !1 !== e.enabled, this.setPerAction("gesture", e), this.setOnEvents("gesture", e), this) : S.bool(e) ? (this.options.gesture.enabled = e, this) : this.options.gesture;
				}, t.map.gesture = We, t.methodDict.gesture = "gesturable", r.actions.gesture = We.defaults;
			},
			listeners: {
				"interactions:action-start": B,
				"interactions:action-move": B,
				"interactions:action-end": B,
				"interactions:new": function(e) {
					e.interaction.gesture = {
						angle: 0,
						distance: 0,
						scale: 1,
						startAngle: 0,
						startDistance: 0
					};
				},
				"auto-start:check": function(e) {
					if (!(e.interaction.pointers.length < 2)) {
						var t = e.interactable.options.gesture;
						if (t && t.enabled) return e.action = { name: "gesture" }, !1;
					}
				}
			},
			defaults: {},
			getCursor: function() {
				return "";
			},
			filterEventType: function(e) {
				return e.search("gesture") === 0;
			}
		}, Ge = We;
		function Ke(e, t, n, r, i, a, o) {
			if (!t) return !1;
			if (!0 === t) {
				var s = S.number(a.width) ? a.width : a.right - a.left, c = S.number(a.height) ? a.height : a.bottom - a.top;
				if (o = Math.min(o, Math.abs((e === "left" || e === "right" ? s : c) / 2)), s < 0 && (e === "left" ? e = "right" : e === "right" && (e = "left")), c < 0 && (e === "top" ? e = "bottom" : e === "bottom" && (e = "top")), e === "left") {
					var l = s >= 0 ? a.left : a.right;
					return n.x < l + o;
				}
				if (e === "top") {
					var u = c >= 0 ? a.top : a.bottom;
					return n.y < u + o;
				}
				if (e === "right") return n.x > (s >= 0 ? a.right : a.left) - o;
				if (e === "bottom") return n.y > (c >= 0 ? a.bottom : a.top) - o;
			}
			return !!S.element(r) && (S.element(t) ? t === r : oe(r, t, i));
		}
		function qe(e) {
			var t = e.iEvent, n = e.interaction;
			if (n.prepared.name === "resize" && n.resizeAxes) {
				var r = t;
				n.interactable.options.resize.square ? (n.resizeAxes === "y" ? r.delta.x = r.delta.y : r.delta.y = r.delta.x, r.axes = "xy") : (r.axes = n.resizeAxes, n.resizeAxes === "x" ? r.delta.y = 0 : n.resizeAxes === "y" && (r.delta.x = 0));
			}
		}
		var V, H, U = {
			id: "actions/resize",
			before: ["actions/drag"],
			install: function(e) {
				var t = e.actions, n = e.browser, r = e.Interactable, i = e.defaults;
				U.cursors = function(e) {
					return e.isIe9 ? {
						x: "e-resize",
						y: "s-resize",
						xy: "se-resize",
						top: "n-resize",
						left: "w-resize",
						bottom: "s-resize",
						right: "e-resize",
						topleft: "se-resize",
						bottomright: "se-resize",
						topright: "ne-resize",
						bottomleft: "ne-resize"
					} : {
						x: "ew-resize",
						y: "ns-resize",
						xy: "nwse-resize",
						top: "ns-resize",
						left: "ew-resize",
						bottom: "ns-resize",
						right: "ew-resize",
						topleft: "nwse-resize",
						bottomright: "nwse-resize",
						topright: "nesw-resize",
						bottomleft: "nesw-resize"
					};
				}(n), U.defaultMargin = n.supportsTouch || n.supportsPointerEvent ? 20 : 10, r.prototype.resizable = function(t) {
					return function(e, t, n) {
						return S.object(t) ? (e.options.resize.enabled = !1 !== t.enabled, e.setPerAction("resize", t), e.setOnEvents("resize", t), S.string(t.axis) && /^x$|^y$|^xy$/.test(t.axis) ? e.options.resize.axis = t.axis : t.axis === null && (e.options.resize.axis = n.defaults.actions.resize.axis), S.bool(t.preserveAspectRatio) ? e.options.resize.preserveAspectRatio = t.preserveAspectRatio : S.bool(t.square) && (e.options.resize.square = t.square), e) : S.bool(t) ? (e.options.resize.enabled = t, e) : e.options.resize;
					}(this, t, e);
				}, t.map.resize = U, t.methodDict.resize = "resizable", i.actions.resize = U.defaults;
			},
			listeners: {
				"interactions:new": function(e) {
					e.interaction.resizeAxes = "xy";
				},
				"interactions:action-start": function(e) {
					(function(e) {
						var t = e.iEvent, n = e.interaction;
						if (n.prepared.name === "resize" && n.prepared.edges) {
							var r = t, i = n.rect;
							n._rects = {
								start: N({}, i),
								corrected: N({}, i),
								previous: N({}, i),
								delta: {
									left: 0,
									right: 0,
									width: 0,
									top: 0,
									bottom: 0,
									height: 0
								}
							}, r.edges = n.prepared.edges, r.rect = n._rects.corrected, r.deltaRect = n._rects.delta;
						}
					})(e), qe(e);
				},
				"interactions:action-move": function(e) {
					(function(e) {
						var t = e.iEvent, n = e.interaction;
						if (n.prepared.name === "resize" && n.prepared.edges) {
							var r = t, i = n.interactable.options.resize.invert, a = i === "reposition" || i === "negate", o = n.rect, s = n._rects, c = s.start, l = s.corrected, u = s.delta, d = s.previous;
							if (N(d, l), a) {
								if (N(l, o), i === "reposition") {
									if (l.top > l.bottom) {
										var f = l.top;
										l.top = l.bottom, l.bottom = f;
									}
									if (l.left > l.right) {
										var p = l.left;
										l.left = l.right, l.right = p;
									}
								}
							} else l.top = Math.min(o.top, c.bottom), l.bottom = Math.max(o.bottom, c.top), l.left = Math.min(o.left, c.right), l.right = Math.max(o.right, c.left);
							for (var m in l.width = l.right - l.left, l.height = l.bottom - l.top, l) u[m] = l[m] - d[m];
							r.edges = n.prepared.edges, r.rect = l, r.deltaRect = u;
						}
					})(e), qe(e);
				},
				"interactions:action-end": function(e) {
					var t = e.iEvent, n = e.interaction;
					if (n.prepared.name === "resize" && n.prepared.edges) {
						var r = t;
						r.edges = n.prepared.edges, r.rect = n._rects.corrected, r.deltaRect = n._rects.delta;
					}
				},
				"auto-start:check": function(e) {
					var t = e.interaction, n = e.interactable, r = e.element, i = e.rect, a = e.buttons;
					if (i) {
						var o = N({}, t.coords.cur.page), s = n.options.resize;
						if (s && s.enabled && (!t.pointerIsDown || !/mouse|pointer/.test(t.pointerType) || (a & s.mouseButtons) != 0)) {
							if (S.object(s.edges)) {
								var c = {
									left: !1,
									right: !1,
									top: !1,
									bottom: !1
								};
								for (var l in c) c[l] = Ke(l, s.edges[l], o, t._latestPointer.eventTarget, r, i, s.margin || U.defaultMargin);
								c.left = c.left && !c.right, c.top = c.top && !c.bottom, (c.left || c.right || c.top || c.bottom) && (e.action = {
									name: "resize",
									edges: c
								});
							} else {
								var u = s.axis !== "y" && o.x > i.right - U.defaultMargin, d = s.axis !== "x" && o.y > i.bottom - U.defaultMargin;
								(u || d) && (e.action = {
									name: "resize",
									axes: (u ? "x" : "") + (d ? "y" : "")
								});
							}
							return !e.action && void 0;
						}
					}
				}
			},
			defaults: {
				square: !1,
				preserveAspectRatio: !1,
				axis: "xy",
				margin: NaN,
				edges: null,
				invert: "none"
			},
			cursors: null,
			getCursor: function(e) {
				var t = e.edges, n = e.axis, r = e.name, i = U.cursors, a = null;
				if (n) a = i[r + n];
				else if (t) {
					for (var o = "", s = 0, c = [
						"top",
						"bottom",
						"left",
						"right"
					]; s < c.length; s++) {
						var l = c[s];
						t[l] && (o += l);
					}
					a = i[o];
				}
				return a;
			},
			filterEventType: function(e) {
				return e.search("resize") === 0;
			},
			defaultMargin: null
		}, Je = U, Ye = {
			id: "actions",
			install: function(e) {
				e.usePlugin(Ge), e.usePlugin(Je), e.usePlugin(T), e.usePlugin(Ue);
			}
		}, Xe = 0, W = {
			request: function(e) {
				return V(e);
			},
			cancel: function(e) {
				return H(e);
			},
			init: function(e) {
				if (V = e.requestAnimationFrame, H = e.cancelAnimationFrame, !V) for (var t = [
					"ms",
					"moz",
					"webkit",
					"o"
				], n = 0; n < t.length; n++) {
					var r = t[n];
					V = e[`${r}RequestAnimationFrame`], H = e[`${r}CancelAnimationFrame`] || e[`${r}CancelRequestAnimationFrame`];
				}
				V &&= V.bind(e), H &&= H.bind(e), V || (V = function(t) {
					var n = Date.now(), r = Math.max(0, 16 - (n - Xe)), i = e.setTimeout((function() {
						t(n + r);
					}), r);
					return Xe = n + r, i;
				}, H = function(e) {
					return clearTimeout(e);
				});
			}
		}, G = {
			defaults: {
				enabled: !1,
				margin: 60,
				container: null,
				speed: 300
			},
			now: Date.now,
			interaction: null,
			i: 0,
			x: 0,
			y: 0,
			isScrolling: !1,
			prevTime: 0,
			margin: 0,
			speed: 0,
			start: function(e) {
				G.isScrolling = !0, W.cancel(G.i), e.autoScroll = G, G.interaction = e, G.prevTime = G.now(), G.i = W.request(G.scroll);
			},
			stop: function() {
				G.isScrolling = !1, G.interaction && (G.interaction.autoScroll = null), W.cancel(G.i);
			},
			scroll: function() {
				var e = G.interaction, t = e.interactable, n = e.element, r = e.prepared.name, i = t.options[r].autoScroll, a = Ze(i.container, t, n), o = G.now(), s = (o - G.prevTime) / 1e3, c = i.speed * s;
				if (c >= 1) {
					var l = {
						x: G.x * c,
						y: G.y * c
					};
					if (l.x || l.y) {
						var u = Qe(a);
						S.window(a) ? a.scrollBy(l.x, l.y) : a && (a.scrollLeft += l.x, a.scrollTop += l.y);
						var d = Qe(a), f = {
							x: d.x - u.x,
							y: d.y - u.y
						};
						(f.x || f.y) && t.fire({
							type: "autoscroll",
							target: n,
							interactable: t,
							delta: f,
							interaction: e,
							container: a
						});
					}
					G.prevTime = o;
				}
				G.isScrolling && (W.cancel(G.i), G.i = W.request(G.scroll));
			},
			check: function(e, t) {
				return e.options[t].autoScroll?.enabled;
			},
			onInteractionMove: function(e) {
				var t = e.interaction, n = e.pointer;
				if (t.interacting() && G.check(t.interactable, t.prepared.name)) {
					if (t.simulation) G.x = G.y = 0;
					else {
						var r, i, a, o, s = t.interactable, c = t.element, l = t.prepared.name, u = s.options[l].autoScroll, d = Ze(u.container, s, c);
						if (S.window(d)) o = n.clientX < G.margin, r = n.clientY < G.margin, i = n.clientX > d.innerWidth - G.margin, a = n.clientY > d.innerHeight - G.margin;
						else {
							var f = ce(d);
							o = n.clientX < f.left + G.margin, r = n.clientY < f.top + G.margin, i = n.clientX > f.right - G.margin, a = n.clientY > f.bottom - G.margin;
						}
						G.x = i ? 1 : o ? -1 : 0, G.y = a ? 1 : r ? -1 : 0, G.isScrolling || (G.margin = u.margin, G.speed = u.speed, G.start(t));
					}
				}
			}
		};
		function Ze(e, t, n) {
			return (S.string(e) ? de(e, t, n) : e) || y(n);
		}
		function Qe(e) {
			return S.window(e) && (e = window.document.body), {
				x: e.scrollLeft,
				y: e.scrollTop
			};
		}
		var $e = {
			id: "auto-scroll",
			install: function(e) {
				var t = e.defaults, n = e.actions;
				e.autoScroll = G, G.now = function() {
					return e.now();
				}, n.phaselessTypes.autoscroll = !0, t.perAction.autoScroll = G.defaults;
			},
			listeners: {
				"interactions:new": function(e) {
					e.interaction.autoScroll = null;
				},
				"interactions:destroy": function(e) {
					e.interaction.autoScroll = null, G.stop(), G.interaction &&= null;
				},
				"interactions:stop": G.stop,
				"interactions:action-move": function(e) {
					return G.onInteractionMove(e);
				}
			}
		};
		function K(e, t) {
			var n = !1;
			return function() {
				return n ||= (_.console.warn(t), !0), e.apply(this, arguments);
			};
		}
		function et(e, t) {
			return e.name = t.name, e.axis = t.axis, e.edges = t.edges, e;
		}
		function tt(e) {
			return S.bool(e) ? (this.options.styleCursor = e, this) : e === null ? (delete this.options.styleCursor, this) : this.options.styleCursor;
		}
		function nt(e) {
			return S.func(e) ? (this.options.actionChecker = e, this) : e === null ? (delete this.options.actionChecker, this) : this.options.actionChecker;
		}
		var rt = {
			id: "auto-start/interactableMethods",
			install: function(e) {
				var t = e.Interactable;
				t.prototype.getAction = function(t, n, r, i) {
					var a = function(e, t, n, r, i) {
						var a = {
							action: null,
							interactable: e,
							interaction: n,
							element: r,
							rect: e.getRect(r),
							buttons: t.buttons || {
								0: 1,
								1: 4,
								3: 8,
								4: 16
							}[t.button]
						};
						return i.fire("auto-start:check", a), a.action;
					}(this, n, r, i, e);
					return this.options.actionChecker ? this.options.actionChecker(t, n, a, this, i, r) : a;
				}, t.prototype.ignoreFrom = K((function(e) {
					return this._backCompatOption("ignoreFrom", e);
				}), "Interactable.ignoreFrom() has been deprecated. Use Interactble.draggable({ignoreFrom: newValue})."), t.prototype.allowFrom = K((function(e) {
					return this._backCompatOption("allowFrom", e);
				}), "Interactable.allowFrom() has been deprecated. Use Interactble.draggable({allowFrom: newValue})."), t.prototype.actionChecker = nt, t.prototype.styleCursor = tt;
			}
		};
		function it(e, t, n, r, i) {
			return t.testIgnoreAllow(t.options[e.name], n, r) && t.options[e.name].enabled && st(t, n, e, i) ? e : null;
		}
		function at(e, t, n, r, i, a, o) {
			for (var s = 0, c = r.length; s < c; s++) {
				var l = r[s], u = i[s], d = l.getAction(t, n, e, u);
				if (d) {
					var f = it(d, l, u, a, o);
					if (f) return {
						action: f,
						interactable: l,
						element: u
					};
				}
			}
			return {
				action: null,
				interactable: null,
				element: null
			};
		}
		function q(e, t, n, r, i) {
			var a = [], o = [], s = r;
			function c(e) {
				a.push(e), o.push(s);
			}
			for (; S.element(s);) {
				a = [], o = [], i.interactables.forEachMatch(s, c);
				var l = at(e, t, n, a, o, r, i);
				if (l.action && !l.interactable.options[l.action.name].manualStart) return l;
				s = A(s);
			}
			return {
				action: null,
				interactable: null,
				element: null
			};
		}
		function ot(e, t, n) {
			var r = t.action, i = t.interactable, a = t.element;
			r ||= { name: null }, e.interactable = i, e.element = a, et(e.prepared, r), e.rect = i && r.name ? i.getRect(a) : null, ut(e, n), n.fire("autoStart:prepared", { interaction: e });
		}
		function st(e, t, n, r) {
			var i = e.options, a = i[n.name].max, o = i[n.name].maxPerElement, s = r.autoStart.maxInteractions, c = 0, l = 0, u = 0;
			if (!(a && o && s)) return !1;
			for (var d = 0, f = r.interactions.list; d < f.length; d++) {
				var p = f[d], m = p.prepared.name;
				if (p.interacting() && (++c >= s || p.interactable === e && ((l += +(m === n.name)) >= a || p.element === t && (u++, m === n.name && u >= o)))) return !1;
			}
			return s > 0;
		}
		function ct(e, t) {
			return S.number(e) ? (t.autoStart.maxInteractions = e, this) : t.autoStart.maxInteractions;
		}
		function lt(e, t, n) {
			var r = n.autoStart.cursorElement;
			r && r !== e && (r.style.cursor = ""), e.ownerDocument.documentElement.style.cursor = t, e.style.cursor = t, n.autoStart.cursorElement = t ? e : null;
		}
		function ut(e, t) {
			var n = e.interactable, r = e.element, i = e.prepared;
			if (e.pointerType === "mouse" && n && n.options.styleCursor) {
				var a = "";
				if (i.name) {
					var o = n.options[i.name].cursorChecker;
					a = S.func(o) ? o(i, n, r, e._interacting) : t.actions.map[i.name].getCursor(i);
				}
				lt(e.element, a || "", t);
			} else t.autoStart.cursorElement && lt(t.autoStart.cursorElement, "", t);
		}
		var dt = {
			id: "auto-start/base",
			before: ["actions"],
			install: function(e) {
				var t = e.interactStatic, n = e.defaults;
				e.usePlugin(rt), n.base.actionChecker = null, n.base.styleCursor = !0, N(n.perAction, {
					manualStart: !1,
					max: 1 / 0,
					maxPerElement: 1,
					allowFrom: null,
					ignoreFrom: null,
					mouseButtons: 1
				}), t.maxInteractions = function(t) {
					return ct(t, e);
				}, e.autoStart = {
					maxInteractions: 1 / 0,
					withinInteractionLimit: st,
					cursorElement: null
				};
			},
			listeners: {
				"interactions:down": function(e, t) {
					var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget;
					n.interacting() || ot(n, q(n, r, i, a, t), t);
				},
				"interactions:move": function(e, t) {
					(function(e, t) {
						var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget;
						n.pointerType !== "mouse" || n.pointerIsDown || n.interacting() || ot(n, q(n, r, i, a, t), t);
					})(e, t), function(e, t) {
						var n = e.interaction;
						if (n.pointerIsDown && !n.interacting() && n.pointerWasMoved && n.prepared.name) {
							t.fire("autoStart:before-start", e);
							var r = n.interactable, i = n.prepared.name;
							i && r && (r.options[i].manualStart || !st(r, n.element, n.prepared, t) ? n.stop() : (n.start(n.prepared, r, n.element), ut(n, t)));
						}
					}(e, t);
				},
				"interactions:stop": function(e, t) {
					var n = e.interaction, r = n.interactable;
					r && r.options.styleCursor && lt(n.element, "", t);
				}
			},
			maxInteractions: ct,
			withinInteractionLimit: st,
			validateAction: it
		}, ft = {
			id: "auto-start/dragAxis",
			listeners: { "autoStart:before-start": function(e, t) {
				var n = e.interaction, r = e.eventTarget, i = e.dx, a = e.dy;
				if (n.prepared.name === "drag") {
					var o = Math.abs(i), s = Math.abs(a), c = n.interactable.options.drag, l = c.startAxis, u = o > s ? "x" : o < s ? "y" : "xy";
					if (n.prepared.axis = c.lockAxis === "start" ? u[0] : c.lockAxis, u !== "xy" && l !== "xy" && l !== u) {
						n.prepared.name = null;
						for (var d = r, f = function(e) {
							if (e !== n.interactable) {
								var i = n.interactable.options.drag;
								if (!i.manualStart && e.testIgnoreAllow(i, d, r)) {
									var a = e.getAction(n.downPointer, n.downEvent, n, d);
									if (a && a.name === "drag" && function(e, t) {
										if (!t) return !1;
										var n = t.options.drag.startAxis;
										return e === "xy" || n === "xy" || n === e;
									}(u, e) && dt.validateAction(a, e, d, r, t)) return e;
								}
							}
						}; S.element(d);) {
							var p = t.interactables.forEachMatch(d, f);
							if (p) {
								n.prepared.name = "drag", n.interactable = p, n.element = d;
								break;
							}
							d = A(d);
						}
					}
				}
			} }
		};
		function pt(e) {
			var t = e.prepared && e.prepared.name;
			if (!t) return null;
			var n = e.interactable.options;
			return n[t].hold || n[t].delay;
		}
		var mt = {
			id: "auto-start/hold",
			install: function(e) {
				var t = e.defaults;
				e.usePlugin(dt), t.perAction.hold = 0, t.perAction.delay = 0;
			},
			listeners: {
				"interactions:new": function(e) {
					e.interaction.autoStartHoldTimer = null;
				},
				"autoStart:prepared": function(e) {
					var t = e.interaction, n = pt(t);
					n > 0 && (t.autoStartHoldTimer = setTimeout((function() {
						t.start(t.prepared, t.interactable, t.element);
					}), n));
				},
				"interactions:move": function(e) {
					var t = e.interaction, n = e.duplicate;
					t.autoStartHoldTimer && t.pointerWasMoved && !n && (clearTimeout(t.autoStartHoldTimer), t.autoStartHoldTimer = null);
				},
				"autoStart:before-start": function(e) {
					var t = e.interaction;
					pt(t) > 0 && (t.prepared.name = null);
				}
			},
			getHoldDuration: pt
		}, ht = {
			id: "auto-start",
			install: function(e) {
				e.usePlugin(dt), e.usePlugin(mt), e.usePlugin(ft);
			}
		}, gt = function(e) {
			return /^(always|never|auto)$/.test(e) ? (this.options.preventDefault = e, this) : S.bool(e) ? (this.options.preventDefault = e ? "always" : "never", this) : this.options.preventDefault;
		};
		function _t(e) {
			var t = e.interaction, n = e.event;
			t.interactable && t.interactable.checkAndPreventDefault(n);
		}
		var vt = {
			id: "core/interactablePreventDefault",
			install: function(e) {
				var t = e.Interactable;
				t.prototype.preventDefault = gt, t.prototype.checkAndPreventDefault = function(t) {
					return function(e, t, n) {
						var r = e.options.preventDefault;
						if (r !== "never") {
							if (r !== "always") {
								if (t.events.supportsPassive && /^touch(start|move)$/.test(n.type)) {
									var i = y(n.target).document, a = t.getDocOptions(i);
									if (!a || !a.events || !1 !== a.events.passive) return;
								}
								/^(mouse|pointer|touch)*(down|start)/i.test(n.type) || S.element(n.target) && j(n.target, "input,select,textarea,[contenteditable=true],[contenteditable=true] *") || n.preventDefault();
							} else n.preventDefault();
						}
					}(this, e, t);
				}, e.interactions.docEvents.push({
					type: "dragstart",
					listener: function(t) {
						for (var n = 0, r = e.interactions.list; n < r.length; n++) {
							var i = r[n];
							if (i.element && (i.element === t.target || ne(i.element, t.target))) return void i.interactable.checkAndPreventDefault(t);
						}
					}
				});
			},
			listeners: [
				"down",
				"move",
				"up",
				"cancel"
			].reduce((function(e, t) {
				return e[`interactions:${t}`] = _t, e;
			}), {})
		};
		function J(e, t) {
			if (t.phaselessTypes[e]) return !0;
			for (var n in t.map) if (e.indexOf(n) === 0 && e.substr(n.length) in t.phases) return !0;
			return !1;
		}
		function Y(e) {
			var t = {};
			for (var n in e) {
				var r = e[n];
				t[n] = S.plainObject(r) ? Y(r) : S.array(r) ? Pe(r) : r;
			}
			return t;
		}
		var yt = function() {
			function e(t) {
				i(this, e), this.states = [], this.startOffset = {
					left: 0,
					right: 0,
					top: 0,
					bottom: 0
				}, this.startDelta = void 0, this.result = void 0, this.endResult = void 0, this.startEdges = void 0, this.edges = void 0, this.interaction = void 0, this.interaction = t, this.result = bt(), this.edges = {
					left: !1,
					right: !1,
					top: !1,
					bottom: !1
				};
			}
			return o(e, [
				{
					key: "start",
					value: function(e, t) {
						var n, r, i = e.phase, a = this.interaction, o = function(e) {
							var t = e.interactable.options[e.prepared.name], n = t.modifiers;
							return n && n.length ? n : [
								"snap",
								"snapSize",
								"snapEdges",
								"restrict",
								"restrictEdges",
								"restrictSize"
							].map((function(e) {
								var n = t[e];
								return n && n.enabled && {
									options: n,
									methods: n._methods
								};
							})).filter((function(e) {
								return !!e;
							}));
						}(a);
						this.prepareStates(o), this.startEdges = N({}, a.edges), this.edges = N({}, this.startEdges), this.startOffset = (n = a.rect, r = t, n ? {
							left: r.x - n.left,
							top: r.y - n.top,
							right: n.right - r.x,
							bottom: n.bottom - r.y
						} : {
							left: 0,
							top: 0,
							right: 0,
							bottom: 0
						}), this.startDelta = {
							x: 0,
							y: 0
						};
						var s = this.fillArg({
							phase: i,
							pageCoords: t,
							preEnd: !1
						});
						return this.result = bt(), this.startAll(s), this.result = this.setAll(s);
					}
				},
				{
					key: "fillArg",
					value: function(e) {
						var t = this.interaction;
						return e.interaction = t, e.interactable = t.interactable, e.element = t.element, e.rect ||= t.rect, e.edges ||= this.startEdges, e.startOffset = this.startOffset, e;
					}
				},
				{
					key: "startAll",
					value: function(e) {
						for (var t = 0, n = this.states; t < n.length; t++) {
							var r = n[t];
							r.methods.start && (e.state = r, r.methods.start(e));
						}
					}
				},
				{
					key: "setAll",
					value: function(e) {
						var t = e.phase, n = e.preEnd, r = e.skipModifiers, i = e.rect, a = e.edges;
						e.coords = N({}, e.pageCoords), e.rect = N({}, i), e.edges = N({}, a);
						for (var o = r ? this.states.slice(r) : this.states, s = bt(e.coords, e.rect), c = 0; c < o.length; c++) {
							var l, u = o[c], d = u.options, f = N({}, e.coords), p = null;
							(l = u.methods) != null && l.set && this.shouldDo(d, n, t) && (e.state = u, p = u.methods.set(e), P(e.edges, e.rect, {
								x: e.coords.x - f.x,
								y: e.coords.y - f.y
							})), s.eventProps.push(p);
						}
						N(this.edges, e.edges), s.delta.x = e.coords.x - e.pageCoords.x, s.delta.y = e.coords.y - e.pageCoords.y, s.rectDelta.left = e.rect.left - i.left, s.rectDelta.right = e.rect.right - i.right, s.rectDelta.top = e.rect.top - i.top, s.rectDelta.bottom = e.rect.bottom - i.bottom;
						var m = this.result.coords, h = this.result.rect;
						return m && h && (s.changed = s.rect.left !== h.left || s.rect.right !== h.right || s.rect.top !== h.top || s.rect.bottom !== h.bottom || m.x !== s.coords.x || m.y !== s.coords.y), s;
					}
				},
				{
					key: "applyToInteraction",
					value: function(e) {
						var t = this.interaction, n = e.phase, r = t.coords.cur, i = t.coords.start, a = this.result, o = this.startDelta, s = a.delta;
						n === "start" && N(this.startDelta, a.delta);
						for (var c = 0, l = [[i, o], [r, s]]; c < l.length; c++) {
							var u = l[c], d = u[0], f = u[1];
							d.page.x += f.x, d.page.y += f.y, d.client.x += f.x, d.client.y += f.y;
						}
						var p = this.result.rectDelta, m = e.rect || t.rect;
						m.left += p.left, m.right += p.right, m.top += p.top, m.bottom += p.bottom, m.width = m.right - m.left, m.height = m.bottom - m.top;
					}
				},
				{
					key: "setAndApply",
					value: function(e) {
						var t = this.interaction, n = e.phase, r = e.preEnd, i = e.skipModifiers, a = this.setAll(this.fillArg({
							preEnd: r,
							phase: n,
							pageCoords: e.modifiedCoords || t.coords.cur.page
						}));
						if (this.result = a, !a.changed && (!i || i < this.states.length) && t.interacting()) return !1;
						if (e.modifiedCoords) {
							var o = t.coords.cur.page, s = {
								x: e.modifiedCoords.x - o.x,
								y: e.modifiedCoords.y - o.y
							};
							a.coords.x += s.x, a.coords.y += s.y, a.delta.x += s.x, a.delta.y += s.y;
						}
						this.applyToInteraction(e);
					}
				},
				{
					key: "beforeEnd",
					value: function(e) {
						var t = e.interaction, n = e.event, r = this.states;
						if (r && r.length) {
							for (var i = !1, a = 0; a < r.length; a++) {
								var o = r[a];
								e.state = o;
								var s = o.options, c = o.methods, l = c.beforeEnd && c.beforeEnd(e);
								if (l) return this.endResult = l, !1;
								i ||= !i && this.shouldDo(s, !0, e.phase, !0);
							}
							i && t.move({
								event: n,
								preEnd: !0
							});
						}
					}
				},
				{
					key: "stop",
					value: function(e) {
						var t = e.interaction;
						if (this.states && this.states.length) {
							var n = N({
								states: this.states,
								interactable: t.interactable,
								element: t.element,
								rect: null
							}, e);
							this.fillArg(n);
							for (var r = 0, i = this.states; r < i.length; r++) {
								var a = i[r];
								n.state = a, a.methods.stop && a.methods.stop(n);
							}
							this.states = null, this.endResult = null;
						}
					}
				},
				{
					key: "prepareStates",
					value: function(e) {
						this.states = [];
						for (var t = 0; t < e.length; t++) {
							var n = e[t], r = n.options, i = n.methods, a = n.name;
							this.states.push({
								options: r,
								methods: i,
								index: t,
								name: a
							});
						}
						return this.states;
					}
				},
				{
					key: "restoreInteractionCoords",
					value: function(e) {
						var t = e.interaction, n = t.coords, r = t.rect, i = t.modification;
						if (i.result) {
							for (var a = i.startDelta, o = i.result, s = o.delta, c = o.rectDelta, l = 0, u = [[n.start, a], [n.cur, s]]; l < u.length; l++) {
								var d = u[l], f = d[0], p = d[1];
								f.page.x -= p.x, f.page.y -= p.y, f.client.x -= p.x, f.client.y -= p.y;
							}
							r.left -= c.left, r.right -= c.right, r.top -= c.top, r.bottom -= c.bottom;
						}
					}
				},
				{
					key: "shouldDo",
					value: function(e, t, n, r) {
						return !(!e || !1 === e.enabled || r && !e.endOnly || e.endOnly && !t || n === "start" && !e.setStart);
					}
				},
				{
					key: "copyFrom",
					value: function(e) {
						this.startOffset = e.startOffset, this.startDelta = e.startDelta, this.startEdges = e.startEdges, this.edges = e.edges, this.states = e.states.map((function(e) {
							return Y(e);
						})), this.result = bt(N({}, e.result.coords), N({}, e.result.rect));
					}
				},
				{
					key: "destroy",
					value: function() {
						for (var e in this) this[e] = null;
					}
				}
			]), e;
		}();
		function bt(e, t) {
			return {
				rect: t,
				coords: e,
				delta: {
					x: 0,
					y: 0
				},
				rectDelta: {
					left: 0,
					right: 0,
					top: 0,
					bottom: 0
				},
				eventProps: [],
				changed: !0
			};
		}
		function X(e, t) {
			var n = e.defaults, r = {
				start: e.start,
				set: e.set,
				beforeEnd: e.beforeEnd,
				stop: e.stop
			}, i = function(e) {
				var i = e || {};
				for (var a in i.enabled = !1 !== i.enabled, n) a in i || (i[a] = n[a]);
				var o = {
					options: i,
					methods: r,
					name: t,
					enable: function() {
						return i.enabled = !0, o;
					},
					disable: function() {
						return i.enabled = !1, o;
					}
				};
				return o;
			};
			return t && typeof t == "string" && (i._defaults = n, i._methods = r), i;
		}
		function Z(e) {
			var t = e.iEvent, n = e.interaction.modification.result;
			n && (t.modifiers = n.eventProps);
		}
		var Q = {
			id: "modifiers/base",
			before: ["actions"],
			install: function(e) {
				e.defaults.perAction.modifiers = [];
			},
			listeners: {
				"interactions:new": function(e) {
					var t = e.interaction;
					t.modification = new yt(t);
				},
				"interactions:before-action-start": function(e) {
					var t = e.interaction, n = e.interaction.modification;
					n.start(e, t.coords.start.page), t.edges = n.edges, n.applyToInteraction(e);
				},
				"interactions:before-action-move": function(e) {
					var t = e.interaction, n = t.modification, r = n.setAndApply(e);
					return t.edges = n.edges, r;
				},
				"interactions:before-action-end": function(e) {
					var t = e.interaction, n = t.modification, r = n.beforeEnd(e);
					return t.edges = n.startEdges, r;
				},
				"interactions:action-start": Z,
				"interactions:action-move": Z,
				"interactions:action-end": Z,
				"interactions:after-action-start": function(e) {
					return e.interaction.modification.restoreInteractionCoords(e);
				},
				"interactions:after-action-move": function(e) {
					return e.interaction.modification.restoreInteractionCoords(e);
				},
				"interactions:stop": function(e) {
					return e.interaction.modification.stop(e);
				}
			}
		}, xt = {
			base: {
				preventDefault: "auto",
				deltaSource: "page"
			},
			perAction: {
				enabled: !1,
				origin: {
					x: 0,
					y: 0
				}
			},
			actions: {}
		}, St = function(e) {
			c(n, e);
			var t = f(n);
			function n(e, r, a, o, s, c, l) {
				var u;
				i(this, n), (u = t.call(this, e)).relatedTarget = null, u.screenX = void 0, u.screenY = void 0, u.button = void 0, u.buttons = void 0, u.ctrlKey = void 0, u.shiftKey = void 0, u.altKey = void 0, u.metaKey = void 0, u.page = void 0, u.client = void 0, u.delta = void 0, u.rect = void 0, u.x0 = void 0, u.y0 = void 0, u.t0 = void 0, u.dt = void 0, u.duration = void 0, u.clientX0 = void 0, u.clientY0 = void 0, u.velocity = void 0, u.speed = void 0, u.swipe = void 0, u.axes = void 0, u.preEnd = void 0, s ||= e.element;
				var f = e.interactable, p = (f && f.options || xt).deltaSource, m = he(f, s, a), h = o === "start", g = o === "end", _ = h ? d(u) : e.prevEvent, v = h ? e.coords.start : g ? {
					page: _.page,
					client: _.client,
					timeStamp: e.coords.cur.timeStamp
				} : e.coords.cur;
				return u.page = N({}, v.page), u.client = N({}, v.client), u.rect = N({}, e.rect), u.timeStamp = v.timeStamp, g || (u.page.x -= m.x, u.page.y -= m.y, u.client.x -= m.x, u.client.y -= m.y), u.ctrlKey = r.ctrlKey, u.altKey = r.altKey, u.shiftKey = r.shiftKey, u.metaKey = r.metaKey, u.button = r.button, u.buttons = r.buttons, u.target = s, u.currentTarget = s, u.preEnd = c, u.type = l || a + (o || ""), u.interactable = f, u.t0 = h ? e.pointers[e.pointers.length - 1].downTime : _.t0, u.x0 = e.coords.start.page.x - m.x, u.y0 = e.coords.start.page.y - m.y, u.clientX0 = e.coords.start.client.x - m.x, u.clientY0 = e.coords.start.client.y - m.y, u.delta = h || g ? {
					x: 0,
					y: 0
				} : {
					x: u[p].x - _[p].x,
					y: u[p].y - _[p].y
				}, u.dt = e.coords.delta.timeStamp, u.duration = u.timeStamp - u.t0, u.velocity = N({}, e.coords.velocity[p]), u.speed = ve(u.velocity.x, u.velocity.y), u.swipe = g || o === "inertiastart" ? u.getSwipe() : null, u;
			}
			return o(n, [
				{
					key: "getSwipe",
					value: function() {
						var e = this._interaction;
						if (e.prevEvent.speed < 600 || this.timeStamp - e.prevEvent.timeStamp > 150) return null;
						var t = 180 * Math.atan2(e.prevEvent.velocityY, e.prevEvent.velocityX) / Math.PI;
						t < 0 && (t += 360);
						var n = 112.5 <= t && t < 247.5, r = 202.5 <= t && t < 337.5;
						return {
							up: r,
							down: !r && 22.5 <= t && t < 157.5,
							left: n,
							right: !n && (292.5 <= t || t < 67.5),
							angle: t,
							speed: e.prevEvent.speed,
							velocity: {
								x: e.prevEvent.velocityX,
								y: e.prevEvent.velocityY
							}
						};
					}
				},
				{
					key: "preventDefault",
					value: function() {}
				},
				{
					key: "stopImmediatePropagation",
					value: function() {
						this.immediatePropagationStopped = this.propagationStopped = !0;
					}
				},
				{
					key: "stopPropagation",
					value: function() {
						this.propagationStopped = !0;
					}
				}
			]), n;
		}(Me);
		Object.defineProperties(St.prototype, {
			pageX: {
				get: function() {
					return this.page.x;
				},
				set: function(e) {
					this.page.x = e;
				}
			},
			pageY: {
				get: function() {
					return this.page.y;
				},
				set: function(e) {
					this.page.y = e;
				}
			},
			clientX: {
				get: function() {
					return this.client.x;
				},
				set: function(e) {
					this.client.x = e;
				}
			},
			clientY: {
				get: function() {
					return this.client.y;
				},
				set: function(e) {
					this.client.y = e;
				}
			},
			dx: {
				get: function() {
					return this.delta.x;
				},
				set: function(e) {
					this.delta.x = e;
				}
			},
			dy: {
				get: function() {
					return this.delta.y;
				},
				set: function(e) {
					this.delta.y = e;
				}
			},
			velocityX: {
				get: function() {
					return this.velocity.x;
				},
				set: function(e) {
					this.velocity.x = e;
				}
			},
			velocityY: {
				get: function() {
					return this.velocity.y;
				},
				set: function(e) {
					this.velocity.y = e;
				}
			}
		});
		var Ct = o((function e(t, n, r, a, o) {
			i(this, e), this.id = void 0, this.pointer = void 0, this.event = void 0, this.downTime = void 0, this.downTarget = void 0, this.id = t, this.pointer = n, this.event = r, this.downTime = a, this.downTarget = o;
		})), wt = function(e) {
			return e.interactable = "", e.element = "", e.prepared = "", e.pointerIsDown = "", e.pointerWasMoved = "", e._proxy = "", e;
		}({}), Tt = function(e) {
			return e.start = "", e.move = "", e.end = "", e.stop = "", e.interacting = "", e;
		}({}), Et = 0, Dt = function() {
			function e(t) {
				var n = this, r = t.pointerType, a = t.scopeFire;
				i(this, e), this.interactable = null, this.element = null, this.rect = null, this._rects = void 0, this.edges = null, this._scopeFire = void 0, this.prepared = {
					name: null,
					axis: null,
					edges: null
				}, this.pointerType = void 0, this.pointers = [], this.downEvent = null, this.downPointer = {}, this._latestPointer = {
					pointer: null,
					event: null,
					eventTarget: null
				}, this.prevEvent = null, this.pointerIsDown = !1, this.pointerWasMoved = !1, this._interacting = !1, this._ending = !1, this._stopped = !0, this._proxy = void 0, this.simulation = null, this.doMove = K((function(e) {
					this.move(e);
				}), "The interaction.doMove() method has been renamed to interaction.move()"), this.coords = {
					start: {
						page: {
							x: 0,
							y: 0
						},
						client: {
							x: 0,
							y: 0
						},
						timeStamp: 0
					},
					prev: {
						page: {
							x: 0,
							y: 0
						},
						client: {
							x: 0,
							y: 0
						},
						timeStamp: 0
					},
					cur: {
						page: {
							x: 0,
							y: 0
						},
						client: {
							x: 0,
							y: 0
						},
						timeStamp: 0
					},
					delta: {
						page: {
							x: 0,
							y: 0
						},
						client: {
							x: 0,
							y: 0
						},
						timeStamp: 0
					},
					velocity: {
						page: {
							x: 0,
							y: 0
						},
						client: {
							x: 0,
							y: 0
						},
						timeStamp: 0
					}
				}, this._id = Et++, this._scopeFire = a, this.pointerType = r;
				var o = this;
				this._proxy = {};
				var s = function(e) {
					Object.defineProperty(n._proxy, e, { get: function() {
						return o[e];
					} });
				};
				for (var c in wt) s(c);
				var l = function(e) {
					Object.defineProperty(n._proxy, e, { value: function() {
						return o[e].apply(o, arguments);
					} });
				};
				for (var u in Tt) l(u);
				this._scopeFire("interactions:new", { interaction: this });
			}
			return o(e, [
				{
					key: "pointerMoveTolerance",
					get: function() {
						return 1;
					}
				},
				{
					key: "pointerDown",
					value: function(e, t, n) {
						var r = this.updatePointer(e, t, n, !0), i = this.pointers[r];
						this._scopeFire("interactions:down", {
							pointer: e,
							event: t,
							eventTarget: n,
							pointerIndex: r,
							pointerInfo: i,
							type: "down",
							interaction: this
						});
					}
				},
				{
					key: "start",
					value: function(e, t, n) {
						return !(this.interacting() || !this.pointerIsDown || this.pointers.length < (e.name === "gesture" ? 2 : 1) || !t.options[e.name].enabled) && (et(this.prepared, e), this.interactable = t, this.element = n, this.rect = t.getRect(n), this.edges = this.prepared.edges ? N({}, this.prepared.edges) : {
							left: !0,
							right: !0,
							top: !0,
							bottom: !0
						}, this._stopped = !1, this._interacting = this._doPhase({
							interaction: this,
							event: this.downEvent,
							phase: "start"
						}) && !this._stopped, this._interacting);
					}
				},
				{
					key: "pointerMove",
					value: function(e, t, n) {
						this.simulation || this.modification && this.modification.endResult || this.updatePointer(e, t, n, !1);
						var r, i, a = this.coords.cur.page.x === this.coords.prev.page.x && this.coords.cur.page.y === this.coords.prev.page.y && this.coords.cur.client.x === this.coords.prev.client.x && this.coords.cur.client.y === this.coords.prev.client.y;
						this.pointerIsDown && !this.pointerWasMoved && (r = this.coords.cur.client.x - this.coords.start.client.x, i = this.coords.cur.client.y - this.coords.start.client.y, this.pointerWasMoved = ve(r, i) > this.pointerMoveTolerance);
						var o, s, c, l = this.getPointerIndex(e), u = {
							pointer: e,
							pointerIndex: l,
							pointerInfo: this.pointers[l],
							event: t,
							type: "move",
							eventTarget: n,
							dx: r,
							dy: i,
							duplicate: a,
							interaction: this
						};
						a || (o = this.coords.velocity, s = this.coords.delta, c = Math.max(s.timeStamp / 1e3, .001), o.page.x = s.page.x / c, o.page.y = s.page.y / c, o.client.x = s.client.x / c, o.client.y = s.client.y / c, o.timeStamp = c), this._scopeFire("interactions:move", u), a || this.simulation || (this.interacting() && (u.type = null, this.move(u)), this.pointerWasMoved && xe(this.coords.prev, this.coords.cur));
					}
				},
				{
					key: "move",
					value: function(e) {
						e && e.event || Se(this.coords.delta), (e = N({
							pointer: this._latestPointer.pointer,
							event: this._latestPointer.event,
							eventTarget: this._latestPointer.eventTarget,
							interaction: this
						}, e || {})).phase = "move", this._doPhase(e);
					}
				},
				{
					key: "pointerUp",
					value: function(e, t, n, r) {
						var i = this.getPointerIndex(e);
						i === -1 && (i = this.updatePointer(e, t, n, !1));
						var a = /cancel$/i.test(t.type) ? "cancel" : "up";
						this._scopeFire(`interactions:${a}`, {
							pointer: e,
							pointerIndex: i,
							pointerInfo: this.pointers[i],
							event: t,
							eventTarget: n,
							type: a,
							curEventTarget: r,
							interaction: this
						}), this.simulation || this.end(t), this.removePointer(e, t);
					}
				},
				{
					key: "documentBlur",
					value: function(e) {
						this.end(e), this._scopeFire("interactions:blur", {
							event: e,
							type: "blur",
							interaction: this
						});
					}
				},
				{
					key: "end",
					value: function(e) {
						var t;
						this._ending = !0, e ||= this._latestPointer.event, this.interacting() && (t = this._doPhase({
							event: e,
							interaction: this,
							phase: "end"
						})), this._ending = !1, !0 === t && this.stop();
					}
				},
				{
					key: "currentAction",
					value: function() {
						return this._interacting ? this.prepared.name : null;
					}
				},
				{
					key: "interacting",
					value: function() {
						return this._interacting;
					}
				},
				{
					key: "stop",
					value: function() {
						this._scopeFire("interactions:stop", { interaction: this }), this.interactable = this.element = null, this._interacting = !1, this._stopped = !0, this.prepared.name = this.prevEvent = null;
					}
				},
				{
					key: "getPointerIndex",
					value: function(e) {
						var t = I(e);
						return this.pointerType === "mouse" || this.pointerType === "pen" ? this.pointers.length - 1 : Fe(this.pointers, (function(e) {
							return e.id === t;
						}));
					}
				},
				{
					key: "getPointerInfo",
					value: function(e) {
						return this.pointers[this.getPointerIndex(e)];
					}
				},
				{
					key: "updatePointer",
					value: function(e, t, n, r) {
						var i, a, o, s = I(e), c = this.getPointerIndex(e), l = this.pointers[c];
						return r = !1 !== r && (r || /(down|start)$/i.test(t.type)), l ? l.pointer = e : (l = new Ct(s, e, t, null, null), c = this.pointers.length, this.pointers.push(l)), Te(this.coords.cur, this.pointers.map((function(e) {
							return e.pointer;
						})), this._now()), i = this.coords.delta, a = this.coords.prev, o = this.coords.cur, i.page.x = o.page.x - a.page.x, i.page.y = o.page.y - a.page.y, i.client.x = o.client.x - a.client.x, i.client.y = o.client.y - a.client.y, i.timeStamp = o.timeStamp - a.timeStamp, r && (this.pointerIsDown = !0, l.downTime = this.coords.cur.timeStamp, l.downTarget = n, be(this.downPointer, e), this.interacting() || (xe(this.coords.start, this.coords.cur), xe(this.coords.prev, this.coords.cur), this.downEvent = t, this.pointerWasMoved = !1)), this._updateLatestPointer(e, t, n), this._scopeFire("interactions:update-pointer", {
							pointer: e,
							event: t,
							eventTarget: n,
							down: r,
							pointerInfo: l,
							pointerIndex: c,
							interaction: this
						}), c;
					}
				},
				{
					key: "removePointer",
					value: function(e, t) {
						var n = this.getPointerIndex(e);
						if (n !== -1) {
							var r = this.pointers[n];
							this._scopeFire("interactions:remove-pointer", {
								pointer: e,
								event: t,
								eventTarget: null,
								pointerIndex: n,
								pointerInfo: r,
								interaction: this
							}), this.pointers.splice(n, 1), this.pointerIsDown = !1;
						}
					}
				},
				{
					key: "_updateLatestPointer",
					value: function(e, t, n) {
						this._latestPointer.pointer = e, this._latestPointer.event = t, this._latestPointer.eventTarget = n;
					}
				},
				{
					key: "destroy",
					value: function() {
						this._latestPointer.pointer = null, this._latestPointer.event = null, this._latestPointer.eventTarget = null;
					}
				},
				{
					key: "_createPreparedEvent",
					value: function(e, t, n, r) {
						return new St(this, e, this.prepared.name, t, this.element, n, r);
					}
				},
				{
					key: "_fireEvent",
					value: function(e) {
						var t;
						(t = this.interactable) == null || t.fire(e), (!this.prevEvent || e.timeStamp >= this.prevEvent.timeStamp) && (this.prevEvent = e);
					}
				},
				{
					key: "_doPhase",
					value: function(e) {
						var t = e.event, n = e.phase, r = e.preEnd, i = e.type, a = this.rect;
						if (a && n === "move" && (P(this.edges, a, this.coords.delta[this.interactable.options.deltaSource]), a.width = a.right - a.left, a.height = a.bottom - a.top), !1 === this._scopeFire(`interactions:before-action-${n}`, e)) return !1;
						var o = e.iEvent = this._createPreparedEvent(t, n, r, i);
						return this._scopeFire(`interactions:action-${n}`, e), n === "start" && (this.prevEvent = o), this._fireEvent(o), this._scopeFire(`interactions:after-action-${n}`, e), !0;
					}
				},
				{
					key: "_now",
					value: function() {
						return Date.now();
					}
				}
			]), e;
		}();
		function Ot(e) {
			kt(e.interaction);
		}
		function kt(e) {
			if (!function(e) {
				return !(!e.offset.pending.x && !e.offset.pending.y);
			}(e)) return !1;
			var t = e.offset.pending;
			return jt(e.coords.cur, t), jt(e.coords.delta, t), P(e.edges, e.rect, t), t.x = 0, t.y = 0, !0;
		}
		function At(e) {
			var t = e.x, n = e.y;
			this.offset.pending.x += t, this.offset.pending.y += n, this.offset.total.x += t, this.offset.total.y += n;
		}
		function jt(e, t) {
			var n = e.page, r = e.client, i = t.x, a = t.y;
			n.x += i, n.y += a, r.x += i, r.y += a;
		}
		Tt.offsetBy = "";
		var Mt = {
			id: "offset",
			before: [
				"modifiers",
				"pointer-events",
				"actions",
				"inertia"
			],
			install: function(e) {
				e.Interaction.prototype.offsetBy = At;
			},
			listeners: {
				"interactions:new": function(e) {
					e.interaction.offset = {
						total: {
							x: 0,
							y: 0
						},
						pending: {
							x: 0,
							y: 0
						}
					};
				},
				"interactions:update-pointer": function(e) {
					return function(e) {
						e.pointerIsDown && (jt(e.coords.cur, e.offset.total), e.offset.pending.x = 0, e.offset.pending.y = 0);
					}(e.interaction);
				},
				"interactions:before-action-start": Ot,
				"interactions:before-action-move": Ot,
				"interactions:before-action-end": function(e) {
					var t = e.interaction;
					if (kt(t)) return t.move({ offset: !0 }), t.end(), !1;
				},
				"interactions:stop": function(e) {
					var t = e.interaction;
					t.offset.total.x = 0, t.offset.total.y = 0, t.offset.pending.x = 0, t.offset.pending.y = 0;
				}
			}
		}, Nt = function() {
			function e(t) {
				i(this, e), this.active = !1, this.isModified = !1, this.smoothEnd = !1, this.allowResume = !1, this.modification = void 0, this.modifierCount = 0, this.modifierArg = void 0, this.startCoords = void 0, this.t0 = 0, this.v0 = 0, this.te = 0, this.targetOffset = void 0, this.modifiedOffset = void 0, this.currentOffset = void 0, this.lambda_v0 = 0, this.one_ve_v0 = 0, this.timeout = void 0, this.interaction = void 0, this.interaction = t;
			}
			return o(e, [
				{
					key: "start",
					value: function(e) {
						var t = this.interaction, n = Pt(t);
						if (!n || !n.enabled) return !1;
						var r = t.coords.velocity.client, i = ve(r.x, r.y), a = this.modification ||= new yt(t);
						if (a.copyFrom(t.modification), this.t0 = t._now(), this.allowResume = n.allowResume, this.v0 = i, this.currentOffset = {
							x: 0,
							y: 0
						}, this.startCoords = t.coords.cur.page, this.modifierArg = a.fillArg({
							pageCoords: this.startCoords,
							preEnd: !0,
							phase: "inertiastart"
						}), this.t0 - t.coords.cur.timeStamp < 50 && i > n.minSpeed && i > n.endSpeed) this.startInertia();
						else {
							if (a.result = a.setAll(this.modifierArg), !a.result.changed) return !1;
							this.startSmoothEnd();
						}
						return t.modification.result.rect = null, t.offsetBy(this.targetOffset), t._doPhase({
							interaction: t,
							event: e,
							phase: "inertiastart"
						}), t.offsetBy({
							x: -this.targetOffset.x,
							y: -this.targetOffset.y
						}), t.modification.result.rect = null, this.active = !0, t.simulation = this, !0;
					}
				},
				{
					key: "startInertia",
					value: function() {
						var e = this, t = this.interaction.coords.velocity.client, n = Pt(this.interaction), r = n.resistance, i = -Math.log(n.endSpeed / this.v0) / r;
						this.targetOffset = {
							x: (t.x - i) / r,
							y: (t.y - i) / r
						}, this.te = i, this.lambda_v0 = r / this.v0, this.one_ve_v0 = 1 - n.endSpeed / this.v0;
						var a = this.modification, o = this.modifierArg;
						o.pageCoords = {
							x: this.startCoords.x + this.targetOffset.x,
							y: this.startCoords.y + this.targetOffset.y
						}, a.result = a.setAll(o), a.result.changed && (this.isModified = !0, this.modifiedOffset = {
							x: this.targetOffset.x + a.result.delta.x,
							y: this.targetOffset.y + a.result.delta.y
						}), this.onNextFrame((function() {
							return e.inertiaTick();
						}));
					}
				},
				{
					key: "startSmoothEnd",
					value: function() {
						var e = this;
						this.smoothEnd = !0, this.isModified = !0, this.targetOffset = {
							x: this.modification.result.delta.x,
							y: this.modification.result.delta.y
						}, this.onNextFrame((function() {
							return e.smoothEndTick();
						}));
					}
				},
				{
					key: "onNextFrame",
					value: function(e) {
						var t = this;
						this.timeout = W.request((function() {
							t.active && e();
						}));
					}
				},
				{
					key: "inertiaTick",
					value: function() {
						var e, t, n, r, i, a, o, s = this, c = this.interaction, l = Pt(c).resistance, u = (c._now() - this.t0) / 1e3;
						if (u < this.te) {
							var d, f = 1 - (Math.exp(-l * u) - this.lambda_v0) / this.one_ve_v0;
							this.isModified ? (e = 0, t = 0, n = this.targetOffset.x, r = this.targetOffset.y, i = this.modifiedOffset.x, a = this.modifiedOffset.y, d = {
								x: It(o = f, e, n, i),
								y: It(o, t, r, a)
							}) : d = {
								x: this.targetOffset.x * f,
								y: this.targetOffset.y * f
							};
							var p = {
								x: d.x - this.currentOffset.x,
								y: d.y - this.currentOffset.y
							};
							this.currentOffset.x += p.x, this.currentOffset.y += p.y, c.offsetBy(p), c.move(), this.onNextFrame((function() {
								return s.inertiaTick();
							}));
						} else c.offsetBy({
							x: this.modifiedOffset.x - this.currentOffset.x,
							y: this.modifiedOffset.y - this.currentOffset.y
						}), this.end();
					}
				},
				{
					key: "smoothEndTick",
					value: function() {
						var e = this, t = this.interaction, n = t._now() - this.t0, r = Pt(t).smoothEndDuration;
						if (n < r) {
							var i = {
								x: Lt(n, 0, this.targetOffset.x, r),
								y: Lt(n, 0, this.targetOffset.y, r)
							}, a = {
								x: i.x - this.currentOffset.x,
								y: i.y - this.currentOffset.y
							};
							this.currentOffset.x += a.x, this.currentOffset.y += a.y, t.offsetBy(a), t.move({ skipModifiers: this.modifierCount }), this.onNextFrame((function() {
								return e.smoothEndTick();
							}));
						} else t.offsetBy({
							x: this.targetOffset.x - this.currentOffset.x,
							y: this.targetOffset.y - this.currentOffset.y
						}), this.end();
					}
				},
				{
					key: "resume",
					value: function(e) {
						var t = e.pointer, n = e.event, r = e.eventTarget, i = this.interaction;
						i.offsetBy({
							x: -this.currentOffset.x,
							y: -this.currentOffset.y
						}), i.updatePointer(t, n, r, !0), i._doPhase({
							interaction: i,
							event: n,
							phase: "resume"
						}), xe(i.coords.prev, i.coords.cur), this.stop();
					}
				},
				{
					key: "end",
					value: function() {
						this.interaction.move(), this.interaction.end(), this.stop();
					}
				},
				{
					key: "stop",
					value: function() {
						this.active = this.smoothEnd = !1, this.interaction.simulation = null, W.cancel(this.timeout);
					}
				}
			]), e;
		}();
		function Pt(e) {
			var t = e.interactable, n = e.prepared;
			return t && t.options && n.name && t.options[n.name].inertia;
		}
		var Ft = {
			id: "inertia",
			before: ["modifiers", "actions"],
			install: function(e) {
				var t = e.defaults;
				e.usePlugin(Mt), e.usePlugin(Q), e.actions.phases.inertiastart = !0, e.actions.phases.resume = !0, t.perAction.inertia = {
					enabled: !1,
					resistance: 10,
					minSpeed: 100,
					endSpeed: 10,
					allowResume: !0,
					smoothEndDuration: 300
				};
			},
			listeners: {
				"interactions:new": function(e) {
					var t = e.interaction;
					t.inertia = new Nt(t);
				},
				"interactions:before-action-end": function(e) {
					var t = e.interaction, n = e.event;
					return (!t._interacting || t.simulation || !t.inertia.start(n)) && null;
				},
				"interactions:down": function(e) {
					var t = e.interaction, n = e.eventTarget, r = t.inertia;
					if (r.active) for (var i = n; S.element(i);) {
						if (i === t.element) {
							r.resume(e);
							break;
						}
						i = A(i);
					}
				},
				"interactions:stop": function(e) {
					var t = e.interaction.inertia;
					t.active && t.stop();
				},
				"interactions:before-action-resume": function(e) {
					var t = e.interaction.modification;
					t.stop(e), t.start(e, e.interaction.coords.cur.page), t.applyToInteraction(e);
				},
				"interactions:before-action-inertiastart": function(e) {
					return e.interaction.modification.setAndApply(e);
				},
				"interactions:action-resume": Z,
				"interactions:action-inertiastart": Z,
				"interactions:after-action-inertiastart": function(e) {
					return e.interaction.modification.restoreInteractionCoords(e);
				},
				"interactions:after-action-resume": function(e) {
					return e.interaction.modification.restoreInteractionCoords(e);
				}
			}
		};
		function It(e, t, n, r) {
			var i = 1 - e;
			return i * i * t + 2 * i * e * n + e * e * r;
		}
		function Lt(e, t, n, r) {
			return -n * (e /= r) * (e - 2) + t;
		}
		var Rt = Ft;
		function zt(e, t) {
			for (var n = 0; n < t.length; n++) {
				var r = t[n];
				if (e.immediatePropagationStopped) break;
				r(e);
			}
		}
		var Bt = function() {
			function e(t) {
				i(this, e), this.options = void 0, this.types = {}, this.propagationStopped = !1, this.immediatePropagationStopped = !1, this.global = void 0, this.options = N({}, t || {});
			}
			return o(e, [
				{
					key: "fire",
					value: function(e) {
						var t, n = this.global;
						(t = this.types[e.type]) && zt(e, t), !e.propagationStopped && n && (t = n[e.type]) && zt(e, t);
					}
				},
				{
					key: "on",
					value: function(e, t) {
						var n = ge(e, t);
						for (e in n) this.types[e] = Ne(this.types[e] || [], n[e]);
					}
				},
				{
					key: "off",
					value: function(e, t) {
						var n = ge(e, t);
						for (e in n) {
							var r = this.types[e];
							if (r && r.length) for (var i = 0, a = n[e]; i < a.length; i++) {
								var o = a[i], s = r.indexOf(o);
								s !== -1 && r.splice(s, 1);
							}
						}
					}
				},
				{
					key: "getRect",
					value: function(e) {
						return null;
					}
				}
			]), e;
		}(), Vt = function() {
			function e(t) {
				i(this, e), this.currentTarget = void 0, this.originalEvent = void 0, this.type = void 0, this.originalEvent = t, be(this, t);
			}
			return o(e, [
				{
					key: "preventOriginalDefault",
					value: function() {
						this.originalEvent.preventDefault();
					}
				},
				{
					key: "stopPropagation",
					value: function() {
						this.originalEvent.stopPropagation();
					}
				},
				{
					key: "stopImmediatePropagation",
					value: function() {
						this.originalEvent.stopImmediatePropagation();
					}
				}
			]), e;
		}();
		function Ht(e) {
			return S.object(e) ? {
				capture: !!e.capture,
				passive: !!e.passive
			} : {
				capture: !!e,
				passive: !1
			};
		}
		function Ut(e, t) {
			return e === t || (typeof e == "boolean" ? !!t.capture === e && !!t.passive == 0 : !!e.capture == !!t.capture && !!e.passive == !!t.passive);
		}
		var Wt = {
			id: "events",
			install: function(e) {
				var t, n = [], r = {}, i = [], a = {
					add: o,
					remove: s,
					addDelegate: function(e, t, n, a, s) {
						var u = Ht(s);
						if (!r[n]) {
							r[n] = [];
							for (var d = 0; d < i.length; d++) {
								var f = i[d];
								o(f, n, c), o(f, n, l, !0);
							}
						}
						var p = r[n], m = Ie(p, (function(n) {
							return n.selector === e && n.context === t;
						}));
						m || (m = {
							selector: e,
							context: t,
							listeners: []
						}, p.push(m)), m.listeners.push({
							func: a,
							options: u
						});
					},
					removeDelegate: function(e, t, n, i, a) {
						var o, u = Ht(a), d = r[n], f = !1;
						if (d) for (o = d.length - 1; o >= 0; o--) {
							var p = d[o];
							if (p.selector === e && p.context === t) {
								for (var m = p.listeners, h = m.length - 1; h >= 0; h--) {
									var g = m[h];
									if (g.func === i && Ut(g.options, u)) {
										m.splice(h, 1), m.length || (d.splice(o, 1), s(t, n, c), s(t, n, l, !0)), f = !0;
										break;
									}
								}
								if (f) break;
							}
						}
					},
					delegateListener: c,
					delegateUseCapture: l,
					delegatedEvents: r,
					documents: i,
					targets: n,
					supportsOptions: !1,
					supportsPassive: !1
				};
				function o(e, t, r, i) {
					if (e.addEventListener) {
						var o = Ht(i), s = Ie(n, (function(t) {
							return t.eventTarget === e;
						}));
						s || (s = {
							eventTarget: e,
							events: {}
						}, n.push(s)), s.events[t] || (s.events[t] = []), Ie(s.events[t], (function(e) {
							return e.func === r && Ut(e.options, o);
						})) || (e.addEventListener(t, r, a.supportsOptions ? o : o.capture), s.events[t].push({
							func: r,
							options: o
						}));
					}
				}
				function s(e, t, r, i) {
					if (e.addEventListener && e.removeEventListener) {
						var o = Fe(n, (function(t) {
							return t.eventTarget === e;
						})), c = n[o];
						if (c && c.events) {
							if (t !== "all") {
								var l = !1, u = c.events[t];
								if (u) {
									if (r === "all") {
										for (var d = u.length - 1; d >= 0; d--) {
											var f = u[d];
											s(e, t, f.func, f.options);
										}
										return;
									}
									for (var p = Ht(i), m = 0; m < u.length; m++) {
										var h = u[m];
										if (h.func === r && Ut(h.options, p)) {
											e.removeEventListener(t, r, a.supportsOptions ? p : p.capture), u.splice(m, 1), u.length === 0 && (delete c.events[t], l = !0);
											break;
										}
									}
								}
								l && !Object.keys(c.events).length && n.splice(o, 1);
							} else for (t in c.events) c.events.hasOwnProperty(t) && s(e, t, "all");
						}
					}
				}
				function c(e, t) {
					for (var n = Ht(t), i = new Vt(e), a = r[e.type], o = je(e)[0], s = o; S.element(s);) {
						for (var c = 0; c < a.length; c++) {
							var l = a[c], u = l.selector, d = l.context;
							if (j(s, u) && ne(d, o) && ne(d, s)) {
								var f = l.listeners;
								i.currentTarget = s;
								for (var p = 0; p < f.length; p++) {
									var m = f[p];
									Ut(m.options, n) && m.func(i);
								}
							}
						}
						s = A(s);
					}
				}
				function l(e) {
					return c(e, !0);
				}
				return (t = e.document) == null || t.createElement("div").addEventListener("test", null, {
					get capture() {
						return a.supportsOptions = !0;
					},
					get passive() {
						return a.supportsPassive = !0;
					}
				}), e.events = a, a;
			}
		}, Gt = {
			methodOrder: [
				"simulationResume",
				"mouseOrPen",
				"hasPointer",
				"idle"
			],
			search: function(e) {
				for (var t = 0, n = Gt.methodOrder; t < n.length; t++) {
					var r = Gt[n[t]](e);
					if (r) return r;
				}
				return null;
			},
			simulationResume: function(e) {
				var t = e.pointerType, n = e.eventType, r = e.eventTarget, i = e.scope;
				if (!/down|start/i.test(n)) return null;
				for (var a = 0, o = i.interactions.list; a < o.length; a++) {
					var s = o[a], c = r;
					if (s.simulation && s.simulation.allowResume && s.pointerType === t) for (; c;) {
						if (c === s.element) return s;
						c = A(c);
					}
				}
				return null;
			},
			mouseOrPen: function(e) {
				var t, n = e.pointerId, r = e.pointerType, i = e.eventType, a = e.scope;
				if (r !== "mouse" && r !== "pen") return null;
				for (var o = 0, s = a.interactions.list; o < s.length; o++) {
					var c = s[o];
					if (c.pointerType === r) {
						if (c.simulation && !Kt(c, n)) continue;
						if (c.interacting()) return c;
						t ||= c;
					}
				}
				if (t) return t;
				for (var l = 0, u = a.interactions.list; l < u.length; l++) {
					var d = u[l];
					if (!(d.pointerType !== r || /down/i.test(i) && d.simulation)) return d;
				}
				return null;
			},
			hasPointer: function(e) {
				for (var t = e.pointerId, n = 0, r = e.scope.interactions.list; n < r.length; n++) {
					var i = r[n];
					if (Kt(i, t)) return i;
				}
				return null;
			},
			idle: function(e) {
				for (var t = e.pointerType, n = 0, r = e.scope.interactions.list; n < r.length; n++) {
					var i = r[n];
					if (i.pointers.length === 1) {
						var a = i.interactable;
						if (a && (!a.options.gesture || !a.options.gesture.enabled)) continue;
					} else if (i.pointers.length >= 2) continue;
					if (!i.interacting() && t === i.pointerType) return i;
				}
				return null;
			}
		};
		function Kt(e, t) {
			return e.pointers.some((function(e) {
				return e.id === t;
			}));
		}
		var qt = Gt, Jt = [
			"pointerDown",
			"pointerMove",
			"pointerUp",
			"updatePointer",
			"removePointer",
			"windowBlur"
		];
		function Yt(e, t) {
			return function(n) {
				var r = t.interactions.list, i = Ae(n), a = je(n), o = a[0], s = a[1], c = [];
				if (/^touch/.test(n.type)) {
					t.prevTouchTime = t.now();
					for (var l = 0, u = n.changedTouches; l < u.length; l++) {
						var d = u[l], f = {
							pointer: d,
							pointerId: I(d),
							pointerType: i,
							eventType: n.type,
							eventTarget: o,
							curEventTarget: s,
							scope: t
						}, p = Xt(f);
						c.push([
							f.pointer,
							f.eventTarget,
							f.curEventTarget,
							p
						]);
					}
				} else {
					var m = !1;
					if (!k.supportsPointerEvent && /mouse/.test(n.type)) {
						for (var h = 0; h < r.length && !m; h++) m = r[h].pointerType !== "mouse" && r[h].pointerIsDown;
						m = m || t.now() - t.prevTouchTime < 500 || n.timeStamp === 0;
					}
					if (!m) {
						var g = {
							pointer: n,
							pointerId: I(n),
							pointerType: i,
							eventType: n.type,
							curEventTarget: s,
							eventTarget: o,
							scope: t
						}, _ = Xt(g);
						c.push([
							g.pointer,
							g.eventTarget,
							g.curEventTarget,
							_
						]);
					}
				}
				for (var v = 0; v < c.length; v++) {
					var y = c[v], b = y[0], x = y[1], S = y[2];
					y[3][e](b, n, x, S);
				}
			};
		}
		function Xt(e) {
			var t = e.pointerType, n = e.scope, r = {
				interaction: qt.search(e),
				searchDetails: e
			};
			return n.fire("interactions:find", r), r.interaction || n.interactions.new({ pointerType: t });
		}
		function Zt(e, t) {
			var n = e.doc, r = e.scope, i = e.options, a = r.interactions.docEvents, o = r.events, s = o[t];
			for (var c in r.browser.isIOS && !i.events && (i.events = { passive: !1 }), o.delegatedEvents) s(n, c, o.delegateListener), s(n, c, o.delegateUseCapture, !0);
			for (var l = i && i.events, u = 0; u < a.length; u++) {
				var d = a[u];
				s(n, d.type, d.listener, l);
			}
		}
		var Qt = {
			id: "core/interactions",
			install: function(e) {
				for (var t = {}, n = 0; n < Jt.length; n++) {
					var r = Jt[n];
					t[r] = Yt(r, e);
				}
				var a, s = k.pEventTypes;
				function l() {
					for (var t = 0, n = e.interactions.list; t < n.length; t++) {
						var r = n[t];
						if (r.pointerIsDown && r.pointerType === "touch" && !r._interacting) for (var i = function() {
							var t = o[a];
							e.documents.some((function(e) {
								return ne(e.doc, t.downTarget);
							})) || r.removePointer(t.pointer, t.event);
						}, a = 0, o = r.pointers; a < o.length; a++) i();
					}
				}
				(a = D.PointerEvent ? [
					{
						type: s.down,
						listener: l
					},
					{
						type: s.down,
						listener: t.pointerDown
					},
					{
						type: s.move,
						listener: t.pointerMove
					},
					{
						type: s.up,
						listener: t.pointerUp
					},
					{
						type: s.cancel,
						listener: t.pointerUp
					}
				] : [
					{
						type: "mousedown",
						listener: t.pointerDown
					},
					{
						type: "mousemove",
						listener: t.pointerMove
					},
					{
						type: "mouseup",
						listener: t.pointerUp
					},
					{
						type: "touchstart",
						listener: l
					},
					{
						type: "touchstart",
						listener: t.pointerDown
					},
					{
						type: "touchmove",
						listener: t.pointerMove
					},
					{
						type: "touchend",
						listener: t.pointerUp
					},
					{
						type: "touchcancel",
						listener: t.pointerUp
					}
				]).push({
					type: "blur",
					listener: function(t) {
						for (var n = 0, r = e.interactions.list; n < r.length; n++) r[n].documentBlur(t);
					}
				}), e.prevTouchTime = 0, e.Interaction = function(t) {
					c(r, t);
					var n = f(r);
					function r() {
						return i(this, r), n.apply(this, arguments);
					}
					return o(r, [{
						key: "pointerMoveTolerance",
						get: function() {
							return e.interactions.pointerMoveTolerance;
						},
						set: function(t) {
							e.interactions.pointerMoveTolerance = t;
						}
					}, {
						key: "_now",
						value: function() {
							return e.now();
						}
					}]), r;
				}(Dt), e.interactions = {
					list: [],
					new: function(t) {
						t.scopeFire = function(t, n) {
							return e.fire(t, n);
						};
						var n = new e.Interaction(t);
						return e.interactions.list.push(n), n;
					},
					listeners: t,
					docEvents: a,
					pointerMoveTolerance: 1
				}, e.usePlugin(vt);
			},
			listeners: {
				"scope:add-document": function(e) {
					return Zt(e, "add");
				},
				"scope:remove-document": function(e) {
					return Zt(e, "remove");
				},
				"interactable:unset": function(e, t) {
					for (var n = e.interactable, r = t.interactions.list.length - 1; r >= 0; r--) {
						var i = t.interactions.list[r];
						i.interactable === n && (i.stop(), t.fire("interactions:destroy", { interaction: i }), i.destroy(), t.interactions.list.length > 2 && t.interactions.list.splice(r, 1));
					}
				}
			},
			onDocSignal: Zt,
			doOnInteractions: Yt,
			methodNames: Jt
		}, $t = function(e) {
			return e[e.On = 0] = "On", e[e.Off = 1] = "Off", e;
		}($t || {}), en = function() {
			function e(t, n, r, a) {
				i(this, e), this.target = void 0, this.options = void 0, this._actions = void 0, this.events = new Bt(), this._context = void 0, this._win = void 0, this._doc = void 0, this._scopeEvents = void 0, this._actions = n.actions, this.target = t, this._context = n.context || r, this._win = y(ue(t) ? this._context : t), this._doc = this._win.document, this._scopeEvents = a, this.set(n);
			}
			return o(e, [
				{
					key: "_defaults",
					get: function() {
						return {
							base: {},
							perAction: {},
							actions: {}
						};
					}
				},
				{
					key: "setOnEvents",
					value: function(e, t) {
						return S.func(t.onstart) && this.on(`${e}start`, t.onstart), S.func(t.onmove) && this.on(`${e}move`, t.onmove), S.func(t.onend) && this.on(`${e}end`, t.onend), S.func(t.oninertiastart) && this.on(`${e}inertiastart`, t.oninertiastart), this;
					}
				},
				{
					key: "updatePerActionListeners",
					value: function(e, t, n) {
						var r = this, i = this._actions.map[e]?.filterEventType, a = function(e) {
							return (i == null || i(e)) && J(e, r._actions);
						};
						(S.array(t) || S.object(t)) && this._onOff($t.Off, e, t, void 0, a), (S.array(n) || S.object(n)) && this._onOff($t.On, e, n, void 0, a);
					}
				},
				{
					key: "setPerAction",
					value: function(e, t) {
						var n = this._defaults;
						for (var r in t) {
							var i = r, a = this.options[e], o = t[i];
							i === "listeners" && this.updatePerActionListeners(e, a.listeners, o), S.array(o) ? a[i] = Pe(o) : S.plainObject(o) ? (a[i] = N(a[i] || {}, Y(o)), S.object(n.perAction[i]) && "enabled" in n.perAction[i] && (a[i].enabled = !1 !== o.enabled)) : S.bool(o) && S.object(n.perAction[i]) ? a[i].enabled = o : a[i] = o;
						}
					}
				},
				{
					key: "getRect",
					value: function(e) {
						return e ||= S.element(this.target) ? this.target : null, S.string(this.target) && (e ||= this._context.querySelector(this.target)), le(e);
					}
				},
				{
					key: "rectChecker",
					value: function(e) {
						var t = this;
						return S.func(e) ? (this.getRect = function(n) {
							var r = N({}, e.call(t, n));
							return "width" in r || (r.width = r.right - r.left, r.height = r.bottom - r.top), r;
						}, this) : e === null ? (delete this.getRect, this) : this.getRect;
					}
				},
				{
					key: "_backCompatOption",
					value: function(e, t) {
						if (ue(t) || S.object(t)) {
							for (var n in this.options[e] = t, this._actions.map) this.options[n][e] = t;
							return this;
						}
						return this.options[e];
					}
				},
				{
					key: "origin",
					value: function(e) {
						return this._backCompatOption("origin", e);
					}
				},
				{
					key: "deltaSource",
					value: function(e) {
						return e === "page" || e === "client" ? (this.options.deltaSource = e, this) : this.options.deltaSource;
					}
				},
				{
					key: "getAllElements",
					value: function() {
						var e = this.target;
						return S.string(e) ? Array.from(this._context.querySelectorAll(e)) : S.func(e) && e.getAllElements ? e.getAllElements() : S.element(e) ? [e] : [];
					}
				},
				{
					key: "context",
					value: function() {
						return this._context;
					}
				},
				{
					key: "inContext",
					value: function(e) {
						return this._context === e.ownerDocument || ne(this._context, e);
					}
				},
				{
					key: "testIgnoreAllow",
					value: function(e, t, n) {
						return !this.testIgnore(e.ignoreFrom, t, n) && this.testAllow(e.allowFrom, t, n);
					}
				},
				{
					key: "testAllow",
					value: function(e, t, n) {
						return !e || !!S.element(n) && (S.string(e) ? oe(n, e, t) : !!S.element(e) && ne(e, n));
					}
				},
				{
					key: "testIgnore",
					value: function(e, t, n) {
						return !(!e || !S.element(n)) && (S.string(e) ? oe(n, e, t) : !!S.element(e) && ne(e, n));
					}
				},
				{
					key: "fire",
					value: function(e) {
						return this.events.fire(e), this;
					}
				},
				{
					key: "_onOff",
					value: function(e, t, n, r, i) {
						S.object(t) && !S.array(t) && (r = n, n = null);
						var a = ge(t, n, i);
						for (var o in a) {
							o === "wheel" && (o = k.wheelEvent);
							for (var s = 0, c = a[o]; s < c.length; s++) {
								var l = c[s];
								J(o, this._actions) ? this.events[e === $t.On ? "on" : "off"](o, l) : S.string(this.target) ? this._scopeEvents[e === $t.On ? "addDelegate" : "removeDelegate"](this.target, this._context, o, l, r) : this._scopeEvents[e === $t.On ? "add" : "remove"](this.target, o, l, r);
							}
						}
						return this;
					}
				},
				{
					key: "on",
					value: function(e, t, n) {
						return this._onOff($t.On, e, t, n);
					}
				},
				{
					key: "off",
					value: function(e, t, n) {
						return this._onOff($t.Off, e, t, n);
					}
				},
				{
					key: "set",
					value: function(e) {
						var t = this._defaults;
						for (var n in S.object(e) || (e = {}), this.options = Y(t.base), this._actions.methodDict) {
							var r = n, i = this._actions.methodDict[r];
							this.options[r] = {}, this.setPerAction(r, N(N({}, t.perAction), t.actions[r])), this[i](e[r]);
						}
						for (var a in e) a === "getRect" ? this.rectChecker(e.getRect) : S.func(this[a]) && this[a](e[a]);
						return this;
					}
				},
				{
					key: "unset",
					value: function() {
						if (S.string(this.target)) for (var e in this._scopeEvents.delegatedEvents) for (var t = this._scopeEvents.delegatedEvents[e], n = t.length - 1; n >= 0; n--) {
							var r = t[n], i = r.selector, a = r.context, o = r.listeners;
							i === this.target && a === this._context && t.splice(n, 1);
							for (var s = o.length - 1; s >= 0; s--) this._scopeEvents.removeDelegate(this.target, this._context, e, o[s][0], o[s][1]);
						}
						else this._scopeEvents.remove(this.target, "all");
					}
				}
			]), e;
		}(), tn = function() {
			function e(t) {
				var n = this;
				i(this, e), this.list = [], this.selectorMap = {}, this.scope = void 0, this.scope = t, t.addListeners({ "interactable:unset": function(e) {
					var t = e.interactable, r = t.target, i = S.string(r) ? n.selectorMap[r] : r[n.scope.id], a = Fe(i, (function(e) {
						return e === t;
					}));
					i.splice(a, 1);
				} });
			}
			return o(e, [
				{
					key: "new",
					value: function(e, t) {
						t = N(t || {}, { actions: this.scope.actions });
						var n = new this.scope.Interactable(e, t, this.scope.document, this.scope.events);
						return this.scope.addDocument(n._doc), this.list.push(n), S.string(e) ? (this.selectorMap[e] || (this.selectorMap[e] = []), this.selectorMap[e].push(n)) : (n.target[this.scope.id] || Object.defineProperty(e, this.scope.id, {
							value: [],
							configurable: !0
						}), e[this.scope.id].push(n)), this.scope.fire("interactable:new", {
							target: e,
							options: t,
							interactable: n,
							win: this.scope._win
						}), n;
					}
				},
				{
					key: "getExisting",
					value: function(e, t) {
						var n = t && t.context || this.scope.document, r = S.string(e), i = r ? this.selectorMap[e] : e[this.scope.id];
						if (i) return Ie(i, (function(t) {
							return t._context === n && (r || t.inContext(e));
						}));
					}
				},
				{
					key: "forEachMatch",
					value: function(e, t) {
						for (var n = 0, r = this.list; n < r.length; n++) {
							var i = r[n], a = void 0;
							if ((S.string(i.target) ? S.element(e) && j(e, i.target) : e === i.target) && i.inContext(e) && (a = t(i)), a !== void 0) return a;
						}
					}
				}
			]), e;
		}(), nn = function() {
			function e() {
				var t = this;
				i(this, e), this.id = `__interact_scope_${Math.floor(100 * Math.random())}`, this.isInitialized = !1, this.listenerMaps = [], this.browser = k, this.defaults = Y(xt), this.Eventable = Bt, this.actions = {
					map: {},
					phases: {
						start: !0,
						move: !0,
						end: !0
					},
					methodDict: {},
					phaselessTypes: {}
				}, this.interactStatic = function(e) {
					var t = function t(n, r) {
						var i = e.interactables.getExisting(n, r);
						return i || ((i = e.interactables.new(n, r)).events.global = t.globalEvents), i;
					};
					return t.getPointerAverage = Ee, t.getTouchBBox = De, t.getTouchDistance = Oe, t.getTouchAngle = ke, t.getElementRect = le, t.getElementClientRect = ce, t.matchesSelector = j, t.closest = re, t.globalEvents = {}, t.version = "1.10.28", t.scope = e, t.use = function(e, t) {
						return this.scope.usePlugin(e, t), this;
					}, t.isSet = function(e, t) {
						return !!this.scope.interactables.get(e, t && t.context);
					}, t.on = K((function(e, t, n) {
						if (S.string(e) && e.search(" ") !== -1 && (e = e.trim().split(/ +/)), S.array(e)) {
							for (var r = 0, i = e; r < i.length; r++) {
								var a = i[r];
								this.on(a, t, n);
							}
							return this;
						}
						if (S.object(e)) {
							for (var o in e) this.on(o, e[o], t);
							return this;
						}
						return J(e, this.scope.actions) ? this.globalEvents[e] ? this.globalEvents[e].push(t) : this.globalEvents[e] = [t] : this.scope.events.add(this.scope.document, e, t, { options: n }), this;
					}), "The interact.on() method is being deprecated"), t.off = K((function(e, t, n) {
						if (S.string(e) && e.search(" ") !== -1 && (e = e.trim().split(/ +/)), S.array(e)) {
							for (var r = 0, i = e; r < i.length; r++) {
								var a = i[r];
								this.off(a, t, n);
							}
							return this;
						}
						if (S.object(e)) {
							for (var o in e) this.off(o, e[o], t);
							return this;
						}
						var s;
						return J(e, this.scope.actions) ? e in this.globalEvents && (s = this.globalEvents[e].indexOf(t)) !== -1 && this.globalEvents[e].splice(s, 1) : this.scope.events.remove(this.scope.document, e, t, n), this;
					}), "The interact.off() method is being deprecated"), t.debug = function() {
						return this.scope;
					}, t.supportsTouch = function() {
						return k.supportsTouch;
					}, t.supportsPointerEvent = function() {
						return k.supportsPointerEvent;
					}, t.stop = function() {
						for (var e = 0, t = this.scope.interactions.list; e < t.length; e++) t[e].stop();
						return this;
					}, t.pointerMoveTolerance = function(e) {
						return S.number(e) ? (this.scope.interactions.pointerMoveTolerance = e, this) : this.scope.interactions.pointerMoveTolerance;
					}, t.addDocument = function(e, t) {
						this.scope.addDocument(e, t);
					}, t.removeDocument = function(e) {
						this.scope.removeDocument(e);
					}, t;
				}(this), this.InteractEvent = St, this.Interactable = void 0, this.interactables = new tn(this), this._win = void 0, this.document = void 0, this.window = void 0, this.documents = [], this._plugins = {
					list: [],
					map: {}
				}, this.onWindowUnload = function(e) {
					return t.removeDocument(e.target);
				};
				var n = this;
				this.Interactable = function(e) {
					c(r, e);
					var t = f(r);
					function r() {
						return i(this, r), t.apply(this, arguments);
					}
					return o(r, [
						{
							key: "_defaults",
							get: function() {
								return n.defaults;
							}
						},
						{
							key: "set",
							value: function(e) {
								return p(l(r.prototype), "set", this).call(this, e), n.fire("interactable:set", {
									options: e,
									interactable: this
								}), this;
							}
						},
						{
							key: "unset",
							value: function() {
								p(l(r.prototype), "unset", this).call(this);
								var e = n.interactables.list.indexOf(this);
								e < 0 || (n.interactables.list.splice(e, 1), n.fire("interactable:unset", { interactable: this }));
							}
						}
					]), r;
				}(en);
			}
			return o(e, [
				{
					key: "addListeners",
					value: function(e, t) {
						this.listenerMaps.push({
							id: t,
							map: e
						});
					}
				},
				{
					key: "fire",
					value: function(e, t) {
						for (var n = 0, r = this.listenerMaps; n < r.length; n++) {
							var i = r[n].map[e];
							if (i && !1 === i(t, this, e)) return !1;
						}
					}
				},
				{
					key: "init",
					value: function(e) {
						return this.isInitialized ? this : function(e, t) {
							return e.isInitialized = !0, S.window(t) && v(t), D.init(t), k.init(t), W.init(t), e.window = t, e.document = t.document, e.usePlugin(Qt), e.usePlugin(Wt), e;
						}(this, e);
					}
				},
				{
					key: "pluginIsInstalled",
					value: function(e) {
						var t = e.id;
						return t ? !!this._plugins.map[t] : this._plugins.list.indexOf(e) !== -1;
					}
				},
				{
					key: "usePlugin",
					value: function(e, t) {
						if (!this.isInitialized || this.pluginIsInstalled(e)) return this;
						if (e.id && (this._plugins.map[e.id] = e), this._plugins.list.push(e), e.install && e.install(this, t), e.listeners && e.before) {
							for (var n = 0, r = this.listenerMaps.length, i = e.before.reduce((function(e, t) {
								return e[t] = !0, e[rn(t)] = !0, e;
							}), {}); n < r; n++) {
								var a = this.listenerMaps[n].id;
								if (a && (i[a] || i[rn(a)])) break;
							}
							this.listenerMaps.splice(n, 0, {
								id: e.id,
								map: e.listeners
							});
						} else e.listeners && this.listenerMaps.push({
							id: e.id,
							map: e.listeners
						});
						return this;
					}
				},
				{
					key: "addDocument",
					value: function(e, t) {
						if (this.getDocIndex(e) !== -1) return !1;
						var n = y(e);
						t = t ? N({}, t) : {}, this.documents.push({
							doc: e,
							options: t
						}), this.events.documents.push(e), e !== this.document && this.events.add(n, "unload", this.onWindowUnload), this.fire("scope:add-document", {
							doc: e,
							window: n,
							scope: this,
							options: t
						});
					}
				},
				{
					key: "removeDocument",
					value: function(e) {
						var t = this.getDocIndex(e), n = y(e), r = this.documents[t].options;
						this.events.remove(n, "unload", this.onWindowUnload), this.documents.splice(t, 1), this.events.documents.splice(t, 1), this.fire("scope:remove-document", {
							doc: e,
							window: n,
							scope: this,
							options: r
						});
					}
				},
				{
					key: "getDocIndex",
					value: function(e) {
						for (var t = 0; t < this.documents.length; t++) if (this.documents[t].doc === e) return t;
						return -1;
					}
				},
				{
					key: "getDocOptions",
					value: function(e) {
						var t = this.getDocIndex(e);
						return t === -1 ? null : this.documents[t].options;
					}
				},
				{
					key: "now",
					value: function() {
						return (this.window.Date || Date).now();
					}
				}
			]), e;
		}();
		function rn(e) {
			return e && e.replace(/\/.*$/, "");
		}
		var an = new nn(), $ = an.interactStatic, on = typeof globalThis < "u" ? globalThis : window;
		an.init(on);
		var sn = Object.freeze({
			__proto__: null,
			edgeTarget: function() {},
			elements: function() {},
			grid: function(e) {
				var t = [
					["x", "y"],
					["left", "top"],
					["right", "bottom"],
					["width", "height"]
				].filter((function(t) {
					var n = t[0], r = t[1];
					return n in e || r in e;
				})), n = function(n, r) {
					for (var i = e.range, a = e.limits, o = a === void 0 ? {
						left: -1 / 0,
						right: 1 / 0,
						top: -1 / 0,
						bottom: 1 / 0
					} : a, s = e.offset, c = s === void 0 ? {
						x: 0,
						y: 0
					} : s, l = {
						range: i,
						grid: e,
						x: null,
						y: null
					}, u = 0; u < t.length; u++) {
						var d = t[u], f = d[0], p = d[1], m = Math.round((n - c.x) / e[f]), h = Math.round((r - c.y) / e[p]);
						l[f] = Math.max(o.left, Math.min(o.right, m * e[f] + c.x)), l[p] = Math.max(o.top, Math.min(o.bottom, h * e[p] + c.y));
					}
					return l;
				};
				return n.grid = e, n.coordFields = t, n;
			}
		}), cn = {
			id: "snappers",
			install: function(e) {
				var t = e.interactStatic;
				t.snappers = N(t.snappers || {}, sn), t.createSnapGrid = t.snappers.grid;
			}
		}, ln = {
			start: function(e) {
				var t = e.state, r = e.rect, i = e.edges, a = e.pageCoords, o = t.options, s = o.ratio, c = o.enabled, l = t.options, u = l.equalDelta, d = l.modifiers;
				s === "preserve" && (s = r.width / r.height), t.startCoords = N({}, a), t.startRect = N({}, r), t.ratio = s, t.equalDelta = u;
				var f = t.linkedEdges = {
					top: i.top || i.left && !i.bottom,
					left: i.left || i.top && !i.right,
					bottom: i.bottom || i.right && !i.top,
					right: i.right || i.bottom && !i.left
				};
				if (t.xIsPrimaryAxis = !(!i.left && !i.right), t.equalDelta) {
					var p = (f.left ? 1 : -1) * (f.top ? 1 : -1);
					t.edgeSign = {
						x: p,
						y: p
					};
				} else t.edgeSign = {
					x: f.left ? -1 : 1,
					y: f.top ? -1 : 1
				};
				if (!1 !== c && N(i, f), d != null && d.length) {
					var m = new yt(e.interaction);
					m.copyFrom(e.interaction.modification), m.prepareStates(d), t.subModification = m, m.startAll(n({}, e));
				}
			},
			set: function(e) {
				var t = e.state, r = e.rect, i = e.coords, a = t.linkedEdges, o = N({}, i), s = t.equalDelta ? un : dn;
				if (N(e.edges, a), s(t, t.xIsPrimaryAxis, i, r), !t.subModification) return null;
				var c = N({}, r);
				P(a, c, {
					x: i.x - o.x,
					y: i.y - o.y
				});
				var l = t.subModification.setAll(n(n({}, e), {}, {
					rect: c,
					edges: a,
					pageCoords: i,
					prevCoords: i,
					prevRect: c
				})), u = l.delta;
				return l.changed && (s(t, Math.abs(u.x) > Math.abs(u.y), l.coords, l.rect), N(i, l.coords)), l.eventProps;
			},
			defaults: {
				ratio: "preserve",
				equalDelta: !1,
				modifiers: [],
				enabled: !1
			}
		};
		function un(e, t, n) {
			var r = e.startCoords, i = e.edgeSign;
			t ? n.y = r.y + (n.x - r.x) * i.y : n.x = r.x + (n.y - r.y) * i.x;
		}
		function dn(e, t, n, r) {
			var i = e.startRect, a = e.startCoords, o = e.ratio, s = e.edgeSign;
			if (t) {
				var c = r.width / o;
				n.y = a.y + (c - i.height) * s.y;
			} else {
				var l = r.height * o;
				n.x = a.x + (l - i.width) * s.x;
			}
		}
		var fn = X(ln, "aspectRatio"), pn = function() {};
		pn._defaults = {};
		var mn = pn;
		function hn(e, t, n) {
			return S.func(e) ? fe(e, t.interactable, t.element, [
				n.x,
				n.y,
				t
			]) : fe(e, t.interactable, t.element);
		}
		var gn = {
			start: function(e) {
				var t = e.rect, n = e.startOffset, r = e.state, i = e.interaction, a = e.pageCoords, o = r.options, s = o.elementRect, c = N({
					left: 0,
					top: 0,
					right: 0,
					bottom: 0
				}, o.offset || {});
				if (t && s) {
					var l = hn(o.restriction, i, a);
					if (l) {
						var u = l.right - l.left - t.width, d = l.bottom - l.top - t.height;
						u < 0 && (c.left += u, c.right += u), d < 0 && (c.top += d, c.bottom += d);
					}
					c.left += n.left - t.width * s.left, c.top += n.top - t.height * s.top, c.right += n.right - t.width * (1 - s.right), c.bottom += n.bottom - t.height * (1 - s.bottom);
				}
				r.offset = c;
			},
			set: function(e) {
				var t = e.coords, n = e.interaction, r = e.state, i = r.options, a = r.offset, o = hn(i.restriction, n, t);
				if (o) {
					var s = function(e) {
						return !e || "left" in e && "top" in e || ((e = N({}, e)).left = e.x || 0, e.top = e.y || 0, e.right = e.right || e.left + e.width, e.bottom = e.bottom || e.top + e.height), e;
					}(o);
					t.x = Math.max(Math.min(s.right - a.right, t.x), s.left + a.left), t.y = Math.max(Math.min(s.bottom - a.bottom, t.y), s.top + a.top);
				}
			},
			defaults: {
				restriction: null,
				elementRect: null,
				offset: null,
				endOnly: !1,
				enabled: !1
			}
		}, _n = X(gn, "restrict"), vn = {
			top: 1 / 0,
			left: 1 / 0,
			bottom: -1 / 0,
			right: -1 / 0
		}, yn = {
			top: -1 / 0,
			left: -1 / 0,
			bottom: 1 / 0,
			right: 1 / 0
		};
		function bn(e, t) {
			for (var n = 0, r = [
				"top",
				"left",
				"bottom",
				"right"
			]; n < r.length; n++) {
				var i = r[n];
				i in e || (e[i] = t[i]);
			}
			return e;
		}
		var xn = {
			noInner: vn,
			noOuter: yn,
			start: function(e) {
				var t, n = e.interaction, r = e.startOffset, i = e.state, a = i.options;
				a && (t = pe(hn(a.offset, n, n.coords.start.page))), t ||= {
					x: 0,
					y: 0
				}, i.offset = {
					top: t.y + r.top,
					left: t.x + r.left,
					bottom: t.y - r.bottom,
					right: t.x - r.right
				};
			},
			set: function(e) {
				var t = e.coords, n = e.edges, r = e.interaction, i = e.state, a = i.offset, o = i.options;
				if (n) {
					var s = N({}, t), c = hn(o.inner, r, s) || {}, l = hn(o.outer, r, s) || {};
					bn(c, vn), bn(l, yn), n.top ? t.y = Math.min(Math.max(l.top + a.top, s.y), c.top + a.top) : n.bottom && (t.y = Math.max(Math.min(l.bottom + a.bottom, s.y), c.bottom + a.bottom)), n.left ? t.x = Math.min(Math.max(l.left + a.left, s.x), c.left + a.left) : n.right && (t.x = Math.max(Math.min(l.right + a.right, s.x), c.right + a.right));
				}
			},
			defaults: {
				inner: null,
				outer: null,
				offset: null,
				endOnly: !1,
				enabled: !1
			}
		}, Sn = X(xn, "restrictEdges"), Cn = N({
			get elementRect() {
				return {
					top: 0,
					left: 0,
					bottom: 1,
					right: 1
				};
			},
			set elementRect(e) {}
		}, gn.defaults), wn = X({
			start: gn.start,
			set: gn.set,
			defaults: Cn
		}, "restrictRect"), Tn = {
			width: -1 / 0,
			height: -1 / 0
		}, En = {
			width: 1 / 0,
			height: 1 / 0
		}, Dn = X({
			start: function(e) {
				return xn.start(e);
			},
			set: function(e) {
				var t = e.interaction, n = e.state, r = e.rect, i = e.edges, a = n.options;
				if (i) {
					var o = me(hn(a.min, t, e.coords)) || Tn, s = me(hn(a.max, t, e.coords)) || En;
					n.options = {
						endOnly: a.endOnly,
						inner: N({}, xn.noInner),
						outer: N({}, xn.noOuter)
					}, i.top ? (n.options.inner.top = r.bottom - o.height, n.options.outer.top = r.bottom - s.height) : i.bottom && (n.options.inner.bottom = r.top + o.height, n.options.outer.bottom = r.top + s.height), i.left ? (n.options.inner.left = r.right - o.width, n.options.outer.left = r.right - s.width) : i.right && (n.options.inner.right = r.left + o.width, n.options.outer.right = r.left + s.width), xn.set(e), n.options = a;
				}
			},
			defaults: {
				min: null,
				max: null,
				endOnly: !1,
				enabled: !1
			}
		}, "restrictSize"), On = {
			start: function(e) {
				var t, n = e.interaction, r = e.interactable, i = e.element, a = e.rect, o = e.state, s = e.startOffset, c = o.options, l = c.offsetWithOrigin ? function(e) {
					var t = e.interaction.element;
					return pe(fe(e.state.options.origin, null, null, [t])) || he(e.interactable, t, e.interaction.prepared.name);
				}(e) : {
					x: 0,
					y: 0
				};
				if (c.offset === "startCoords") t = {
					x: n.coords.start.page.x,
					y: n.coords.start.page.y
				};
				else {
					var u = fe(c.offset, r, i, [n]);
					(t = pe(u) || {
						x: 0,
						y: 0
					}).x += l.x, t.y += l.y;
				}
				var d = c.relativePoints;
				o.offsets = a && d && d.length ? d.map((function(e, n) {
					return {
						index: n,
						relativePoint: e,
						x: s.left - a.width * e.x + t.x,
						y: s.top - a.height * e.y + t.y
					};
				})) : [{
					index: 0,
					relativePoint: null,
					x: t.x,
					y: t.y
				}];
			},
			set: function(e) {
				var t = e.interaction, n = e.coords, r = e.state, i = r.options, a = r.offsets, o = he(t.interactable, t.element, t.prepared.name), s = N({}, n), c = [];
				i.offsetWithOrigin || (s.x -= o.x, s.y -= o.y);
				for (var l = 0, u = a; l < u.length; l++) for (var d = u[l], f = s.x - d.x, p = s.y - d.y, m = 0, h = i.targets.length; m < h; m++) {
					var g = i.targets[m], _ = void 0;
					(_ = S.func(g) ? g(f, p, t._proxy, d, m) : g) && c.push({
						x: (S.number(_.x) ? _.x : f) + d.x,
						y: (S.number(_.y) ? _.y : p) + d.y,
						range: S.number(_.range) ? _.range : i.range,
						source: g,
						index: m,
						offset: d
					});
				}
				for (var v = {
					target: null,
					inRange: !1,
					distance: 0,
					range: 0,
					delta: {
						x: 0,
						y: 0
					}
				}, y = 0; y < c.length; y++) {
					var b = c[y], x = b.range, ee = b.x - s.x, C = b.y - s.y, w = ve(ee, C), T = w <= x;
					x === 1 / 0 && v.inRange && v.range !== 1 / 0 && (T = !1), v.target && !(T ? v.inRange && x !== 1 / 0 ? w / x < v.distance / v.range : x === 1 / 0 && v.range !== 1 / 0 || w < v.distance : !v.inRange && w < v.distance) || (v.target = b, v.distance = w, v.range = x, v.inRange = T, v.delta.x = ee, v.delta.y = C);
				}
				return v.inRange && (n.x = v.target.x, n.y = v.target.y), r.closest = v, v;
			},
			defaults: {
				range: 1 / 0,
				targets: null,
				offset: null,
				offsetWithOrigin: !0,
				origin: null,
				relativePoints: null,
				endOnly: !1,
				enabled: !1
			}
		}, kn = X(On, "snap"), An = {
			start: function(e) {
				var t = e.state, n = e.edges, r = t.options;
				if (!n) return null;
				e.state = { options: {
					targets: null,
					relativePoints: [{
						x: +!n.left,
						y: +!n.top
					}],
					offset: r.offset || "self",
					origin: {
						x: 0,
						y: 0
					},
					range: r.range
				} }, t.targetFields = t.targetFields || [["width", "height"], ["x", "y"]], On.start(e), t.offsets = e.state.offsets, e.state = t;
			},
			set: function(e) {
				var t = e.interaction, n = e.state, r = e.coords, i = n.options, a = n.offsets, o = {
					x: r.x - a[0].x,
					y: r.y - a[0].y
				};
				n.options = N({}, i), n.options.targets = [];
				for (var s = 0, c = i.targets || []; s < c.length; s++) {
					var l = c[s], u = void 0;
					if (u = S.func(l) ? l(o.x, o.y, t) : l) {
						for (var d = 0, f = n.targetFields; d < f.length; d++) {
							var p = f[d], m = p[0], h = p[1];
							if (m in u || h in u) {
								u.x = u[m], u.y = u[h];
								break;
							}
						}
						n.options.targets.push(u);
					}
				}
				var g = On.set(e);
				return n.options = i, g;
			},
			defaults: {
				range: 1 / 0,
				targets: null,
				offset: null,
				endOnly: !1,
				enabled: !1
			}
		}, jn = X(An, "snapSize"), Mn = {
			aspectRatio: fn,
			restrictEdges: Sn,
			restrict: _n,
			restrictRect: wn,
			restrictSize: Dn,
			snapEdges: X({
				start: function(e) {
					var t = e.edges;
					return t ? (e.state.targetFields = e.state.targetFields || [[t.left ? "left" : "right", t.top ? "top" : "bottom"]], An.start(e)) : null;
				},
				set: An.set,
				defaults: N(Y(An.defaults), {
					targets: void 0,
					range: void 0,
					offset: {
						x: 0,
						y: 0
					}
				})
			}, "snapEdges"),
			snap: kn,
			snapSize: jn,
			spring: mn,
			avoid: mn,
			transform: mn,
			rubberband: mn
		}, Nn = {
			id: "modifiers",
			install: function(e) {
				var t = e.interactStatic;
				for (var n in e.usePlugin(Q), e.usePlugin(cn), t.modifiers = Mn, Mn) {
					var r = Mn[n], i = r._defaults;
					i._methods = r._methods, e.defaults.perAction[n] = i;
				}
			}
		}, Pn = function(e) {
			c(n, e);
			var t = f(n);
			function n(e, r, a, o, s, c) {
				var l;
				if (i(this, n), be(d(l = t.call(this, s)), a), a !== r && be(d(l), r), l.timeStamp = c, l.originalEvent = a, l.type = e, l.pointerId = I(r), l.pointerType = Ae(r), l.target = o, l.currentTarget = null, e === "tap") {
					var u = s.getPointerIndex(r);
					l.dt = l.timeStamp - s.pointers[u].downTime;
					var f = l.timeStamp - s.tapTime;
					l.double = !!s.prevTap && s.prevTap.type !== "doubletap" && s.prevTap.target === l.target && f < 500;
				} else e === "doubletap" && (l.dt = r.timeStamp - s.tapTime, l.double = !0);
				return l;
			}
			return o(n, [
				{
					key: "_subtractOrigin",
					value: function(e) {
						var t = e.x, n = e.y;
						return this.pageX -= t, this.pageY -= n, this.clientX -= t, this.clientY -= n, this;
					}
				},
				{
					key: "_addOrigin",
					value: function(e) {
						var t = e.x, n = e.y;
						return this.pageX += t, this.pageY += n, this.clientX += t, this.clientY += n, this;
					}
				},
				{
					key: "preventDefault",
					value: function() {
						this.originalEvent.preventDefault();
					}
				}
			]), n;
		}(Me), Fn = {
			id: "pointer-events/base",
			before: [
				"inertia",
				"modifiers",
				"auto-start",
				"actions"
			],
			install: function(e) {
				e.pointerEvents = Fn, e.defaults.actions.pointerEvents = Fn.defaults, N(e.actions.phaselessTypes, Fn.types);
			},
			listeners: {
				"interactions:new": function(e) {
					var t = e.interaction;
					t.prevTap = null, t.tapTime = 0;
				},
				"interactions:update-pointer": function(e) {
					var t = e.down, n = e.pointerInfo;
					(t || !n.hold) && (n.hold = {
						duration: 1 / 0,
						timeout: null
					});
				},
				"interactions:move": function(e, t) {
					var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget;
					e.duplicate || n.pointerIsDown && !n.pointerWasMoved || (n.pointerIsDown && Rn(e), In({
						interaction: n,
						pointer: r,
						event: i,
						eventTarget: a,
						type: "move"
					}, t));
				},
				"interactions:down": function(e, t) {
					(function(e, t) {
						for (var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget, o = e.pointerIndex, s = n.pointers[o].hold, c = M(a), l = {
							interaction: n,
							pointer: r,
							event: i,
							eventTarget: a,
							type: "hold",
							targets: [],
							path: c,
							node: null
						}, u = 0; u < c.length; u++) l.node = c[u], t.fire("pointerEvents:collect-targets", l);
						if (l.targets.length) {
							for (var d = 1 / 0, f = 0, p = l.targets; f < p.length; f++) {
								var m = p[f].eventable.options.holdDuration;
								m < d && (d = m);
							}
							s.duration = d, s.timeout = setTimeout((function() {
								In({
									interaction: n,
									eventTarget: a,
									pointer: r,
									event: i,
									type: "hold"
								}, t);
							}), d);
						}
					})(e, t), In(e, t);
				},
				"interactions:up": function(e, t) {
					Rn(e), In(e, t), function(e, t) {
						var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget;
						n.pointerWasMoved || In({
							interaction: n,
							eventTarget: a,
							pointer: r,
							event: i,
							type: "tap"
						}, t);
					}(e, t);
				},
				"interactions:cancel": function(e, t) {
					Rn(e), In(e, t);
				}
			},
			PointerEvent: Pn,
			fire: In,
			collectEventTargets: Ln,
			defaults: {
				holdDuration: 600,
				ignoreFrom: null,
				allowFrom: null,
				origin: {
					x: 0,
					y: 0
				}
			},
			types: {
				down: !0,
				move: !0,
				up: !0,
				cancel: !0,
				tap: !0,
				doubletap: !0,
				hold: !0
			}
		};
		function In(e, t) {
			var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget, o = e.type, s = e.targets, c = s === void 0 ? Ln(e, t) : s, l = new Pn(o, r, i, a, n, t.now());
			t.fire("pointerEvents:new", { pointerEvent: l });
			for (var u = {
				interaction: n,
				pointer: r,
				event: i,
				eventTarget: a,
				targets: c,
				type: o,
				pointerEvent: l
			}, d = 0; d < c.length; d++) {
				var f = c[d];
				for (var p in f.props || {}) l[p] = f.props[p];
				var m = he(f.eventable, f.node);
				if (l._subtractOrigin(m), l.eventable = f.eventable, l.currentTarget = f.node, f.eventable.fire(l), l._addOrigin(m), l.immediatePropagationStopped || l.propagationStopped && d + 1 < c.length && c[d + 1].node !== l.currentTarget) break;
			}
			if (t.fire("pointerEvents:fired", u), o === "tap") {
				var h = l.double ? In({
					interaction: n,
					pointer: r,
					event: i,
					eventTarget: a,
					type: "doubletap"
				}, t) : l;
				n.prevTap = h, n.tapTime = h.timeStamp;
			}
			return l;
		}
		function Ln(e, t) {
			var n = e.interaction, r = e.pointer, i = e.event, a = e.eventTarget, o = e.type, s = n.getPointerIndex(r), c = n.pointers[s];
			if (o === "tap" && (n.pointerWasMoved || !c || c.downTarget !== a)) return [];
			for (var l = M(a), u = {
				interaction: n,
				pointer: r,
				event: i,
				eventTarget: a,
				type: o,
				path: l,
				targets: [],
				node: null
			}, d = 0; d < l.length; d++) u.node = l[d], t.fire("pointerEvents:collect-targets", u);
			return o === "hold" && (u.targets = u.targets.filter((function(e) {
				var t, r;
				return e.eventable.options.holdDuration === ((t = n.pointers[s]) == null || (r = t.hold) == null ? void 0 : r.duration);
			}))), u.targets;
		}
		function Rn(e) {
			var t = e.interaction, n = e.pointerIndex, r = t.pointers[n].hold;
			r && r.timeout && (clearTimeout(r.timeout), r.timeout = null);
		}
		var zn = Object.freeze({
			__proto__: null,
			default: Fn
		});
		function Bn(e) {
			var t = e.interaction;
			t.holdIntervalHandle &&= (clearInterval(t.holdIntervalHandle), null);
		}
		var Vn = {
			id: "pointer-events/holdRepeat",
			install: function(e) {
				e.usePlugin(Fn);
				var t = e.pointerEvents;
				t.defaults.holdRepeatInterval = 0, t.types.holdrepeat = e.actions.phaselessTypes.holdrepeat = !0;
			},
			listeners: [
				"move",
				"up",
				"cancel",
				"endall"
			].reduce((function(e, t) {
				return e[`pointerEvents:${t}`] = Bn, e;
			}), {
				"pointerEvents:new": function(e) {
					var t = e.pointerEvent;
					t.type === "hold" && (t.count = (t.count || 0) + 1);
				},
				"pointerEvents:fired": function(e, t) {
					var n = e.interaction, r = e.pointerEvent, i = e.eventTarget, a = e.targets;
					if (r.type === "hold" && a.length) {
						var o = a[0].eventable.options.holdRepeatInterval;
						o <= 0 || (n.holdIntervalHandle = setTimeout((function() {
							t.pointerEvents.fire({
								interaction: n,
								eventTarget: i,
								type: "hold",
								pointer: r,
								event: r
							}, t);
						}), o));
					}
				}
			})
		}, Hn = {
			id: "pointer-events/interactableTargets",
			install: function(e) {
				var t = e.Interactable;
				t.prototype.pointerEvents = function(e) {
					return N(this.events.options, e), this;
				};
				var n = t.prototype._backCompatOption;
				t.prototype._backCompatOption = function(e, t) {
					var r = n.call(this, e, t);
					return r === this && (this.events.options[e] = t), r;
				};
			},
			listeners: {
				"pointerEvents:collect-targets": function(e, t) {
					var n = e.targets, r = e.node, i = e.type, a = e.eventTarget;
					t.interactables.forEachMatch(r, (function(e) {
						var t = e.events, o = t.options;
						t.types[i] && t.types[i].length && e.testIgnoreAllow(o, r, a) && n.push({
							node: r,
							eventable: t,
							props: { interactable: e }
						});
					}));
				},
				"interactable:new": function(e) {
					var t = e.interactable;
					t.events.getRect = function(e) {
						return t.getRect(e);
					};
				},
				"interactable:set": function(e, t) {
					var n = e.interactable, r = e.options;
					N(n.events.options, t.pointerEvents.defaults), N(n.events.options, r.pointerEvents || {});
				}
			}
		};
		if ($.use(vt), $.use(Mt), $.use({
			id: "pointer-events",
			install: function(e) {
				e.usePlugin(zn), e.usePlugin(Vn), e.usePlugin(Hn);
			}
		}), $.use(Rt), $.use(Nn), $.use(ht), $.use(Ye), $.use($e), $.use({
			id: "reflow",
			install: function(e) {
				var t = e.Interactable;
				e.actions.phases.reflow = !0, t.prototype.reflow = function(t) {
					return function(e, t, n) {
						for (var r = e.getAllElements(), i = n.window.Promise, a = i ? [] : null, o = function() {
							var o = r[s], c = e.getRect(o);
							if (!c) return 1;
							var l, u = Ie(n.interactions.list, (function(n) {
								return n.interacting() && n.interactable === e && n.element === o && n.prepared.name === t.name;
							}));
							if (u) u.move(), a && (l = u._reflowPromise || new i((function(e) {
								u._reflowResolve = e;
							})));
							else {
								var d = me(c);
								l = function(e, t, n, r, i) {
									var a = e.interactions.new({ pointerType: "reflow" }), o = {
										interaction: a,
										event: i,
										pointer: i,
										eventTarget: n,
										phase: "reflow"
									};
									a.interactable = t, a.element = n, a.prevEvent = i, a.updatePointer(i, i, n, !0), Se(a.coords.delta), et(a.prepared, r), a._doPhase(o);
									var s = e.window.Promise, c = s ? new s((function(e) {
										a._reflowResolve = e;
									})) : void 0;
									return a._reflowPromise = c, a.start(r, t, n), a._interacting ? (a.move(o), a.end(i)) : (a.stop(), a._reflowResolve()), a.removePointer(i, i), c;
								}(n, e, o, t, function(e) {
									return {
										coords: e,
										get page() {
											return this.coords.page;
										},
										get client() {
											return this.coords.client;
										},
										get timeStamp() {
											return this.coords.timeStamp;
										},
										get pageX() {
											return this.coords.page.x;
										},
										get pageY() {
											return this.coords.page.y;
										},
										get clientX() {
											return this.coords.client.x;
										},
										get clientY() {
											return this.coords.client.y;
										},
										get pointerId() {
											return this.coords.pointerId;
										},
										get target() {
											return this.coords.target;
										},
										get type() {
											return this.coords.type;
										},
										get pointerType() {
											return this.coords.pointerType;
										},
										get buttons() {
											return this.coords.buttons;
										},
										preventDefault: function() {}
									};
								}({
									page: {
										x: d.x,
										y: d.y
									},
									client: {
										x: d.x,
										y: d.y
									},
									timeStamp: n.now()
								}));
							}
							a && a.push(l);
						}, s = 0; s < r.length && !o(); s++);
						return a && i.all(a).then((function() {
							return e;
						}));
					}(this, t, e);
				};
			},
			listeners: { "interactions:stop": function(e, t) {
				var n = e.interaction;
				n.pointerType === "reflow" && (n._reflowResolve && n._reflowResolve(), function(e, t) {
					e.splice(e.indexOf(t), 1);
				}(t.interactions.list, n));
			} }
		}), $.default = $, (t === void 0 ? "undefined" : r(t)) === "object" && t) try {
			t.exports = $;
		} catch {}
		return $.default = $, $;
	}));
})))(), 1);
function l(e) {
	switch (e.status) {
		case "queued":
		case "running": return "working";
		case "failed": return "failed";
		default: return e.waiting_for ? "waiting" : e.status === "success" ? e.stale_reason ? "stale" : e.manual ? "manual" : "done" : "idle";
	}
}
function u(e) {
	let t = l(e), n = {
		idle: "not started",
		waiting: `waiting for ${e.waiting_for ?? ""}`,
		working: e.status === "running" ? "working" : "queued",
		done: "done",
		stale: "stale",
		manual: "done (manual settings)",
		failed: "failed"
	}, r = "";
	return t === "failed" && e.last_error ? r = `: ${e.last_error}` : t === "stale" && e.stale_reason && (r = ` (${e.stale_reason})`), `${e.label}: ${n[t]}${r}`;
}
function d(e, t, n, r = 196, i = 4) {
	if (n <= 0) return [0, 0];
	let a = Math.min(n, Math.max(0, Math.floor(e / r) - i)), o = Math.min(n, Math.ceil((e + t) / r) + i);
	return [a, Math.max(a, o)];
}
function f(e, t) {
	let n = e.map((e) => e.id);
	if (t === "natural") return n;
	let r = (e) => {
		let n = t === "width" ? e.width : e.height;
		return n === null ? Infinity : n;
	};
	return e.map((e, t) => ({
		item: e,
		index: t
	})).sort((e, t) => r(e.item) - r(t.item) || e.index - t.index).map(({ item: e }) => e.id);
}
function p(e, t, n, r) {
	if (r.shift && e.anchor && t.includes(e.anchor)) {
		let r = t.indexOf(e.anchor), i = t.indexOf(n), [a, o] = r <= i ? [r, i] : [i, r];
		return {
			selected: new Set(t.slice(a, o + 1)),
			anchor: e.anchor
		};
	}
	if (r.ctrl) {
		let t = new Set(e.selected);
		return t.has(n) && t.size > 1 ? t.delete(n) : t.add(n), {
			selected: t,
			anchor: n
		};
	}
	return {
		selected: /* @__PURE__ */ new Set([n]),
		anchor: n
	};
}
function m(e, t, n) {
	if (e.length === 0) return null;
	let r = t ? e.indexOf(t) : -1;
	return r < 0 ? e[0] ?? null : e[Math.min(e.length - 1, Math.max(0, r + n))] ?? null;
}
var h = class {
	scroller;
	callbacks;
	items = /* @__PURE__ */ new Map();
	order = [];
	active = null;
	selected = /* @__PURE__ */ new Set();
	spacer;
	frame = 0;
	constructor(e, t) {
		this.scroller = e, this.callbacks = t, this.spacer = document.createElement("div"), this.spacer.className = "st-strip-spacer", this.scroller.replaceChildren(this.spacer), this.scroller.addEventListener("scroll", () => this.schedule(), { passive: !0 }), typeof ResizeObserver < "u" && new ResizeObserver(() => this.schedule()).observe(this.scroller);
	}
	setItems(e, t) {
		this.items = new Map(e.map((e) => [e.id, e])), this.order = t.filter((e) => this.items.has(e)), this.spacer.style.height = `${this.order.length * 196}px`, this.render();
	}
	setOrder(e) {
		this.order = e.filter((e) => this.items.has(e)), this.render();
	}
	update(e) {
		this.items.set(e.id, e);
		let t = this.spacer.querySelector(`[data-strip-page="${e.id}"]`);
		t && this.fill(t, e);
	}
	setActive(e, t, n) {
		this.active = e, this.selected = t, n && e && this.scrollTo(e), this.render();
	}
	displayOrder() {
		return [...this.order];
	}
	scrollTo(e) {
		let t = this.order.indexOf(e);
		if (t < 0) return;
		let n = t * 196, r = n + 196;
		n < this.scroller.scrollTop ? this.scroller.scrollTop = n : r > this.scroller.scrollTop + this.scroller.clientHeight && (this.scroller.scrollTop = r - this.scroller.clientHeight);
	}
	schedule() {
		if (this.frame) return;
		let e = () => {
			this.frame = 0, this.render();
		};
		this.frame = typeof requestAnimationFrame == "function" ? requestAnimationFrame(e) : (e(), 0);
	}
	render() {
		let [e, t] = d(this.scroller.scrollTop, this.scroller.clientHeight || 784, this.order.length), n = new Set(this.order.slice(e, t));
		for (let e of [...this.spacer.children]) n.has(e.dataset.stripPage ?? "") || e.remove();
		for (let n = e; n < t; n += 1) {
			let e = this.order[n], t = e ? this.items.get(e) : void 0;
			if (!e || !t) continue;
			let r = this.spacer.querySelector(`[data-strip-page="${e}"]`);
			r || (r = this.createRow(e), this.spacer.append(r)), r.style.transform = `translateY(${n * 196}px)`, this.fill(r, t);
		}
	}
	createRow(e) {
		let t = document.createElement("div");
		t.className = "st-strip-item", t.dataset.stripPage = e, t.setAttribute("role", "option");
		let n = document.createElement("div");
		n.className = "st-strip-thumb";
		let r = document.createElement("img");
		r.loading = "lazy", r.decoding = "async", r.draggable = !1, r.alt = "", n.append(r);
		let i = document.createElement("div");
		i.className = "st-strip-caption";
		let a = document.createElement("span");
		return a.className = "st-strip-icons", t.append(n, i, a), t.addEventListener("click", (t) => {
			this.callbacks.onClick(e, {
				ctrl: t.ctrlKey || t.metaKey,
				shift: t.shiftKey
			});
		}), t;
	}
	fill(e, t) {
		e.classList.toggle("active", t.id === this.active), e.classList.toggle("selected", this.selected.has(t.id)), e.setAttribute("aria-selected", String(this.selected.has(t.id))), e.title = t.caption ? `${t.label} · ${t.caption}` : t.label;
		let n = e.querySelector("img");
		n && n.getAttribute("src") !== t.thumbnailUrl && (t.thumbnailUrl ? n.src = t.thumbnailUrl : n.removeAttribute("src"), n.alt = t.label);
		let r = e.querySelector(".st-strip-caption");
		r && (r.textContent = t.caption ? `${t.label} · ${t.caption}` : t.label);
		let i = e.querySelector(".st-strip-icons");
		i && i.replaceChildren(...t.stages.map((e, t) => {
			let n = document.createElement("span"), r = l(e);
			return n.className = `st-status-icon st-status-${r}`, n.dataset.stage = e.stage, n.dataset.status = e.status, n.dataset.kind = r, n.textContent = String(t + 1), n.title = u(e), n;
		}));
	}
};
function g(e, t, n, r, i, a) {
	let o = { ...e };
	return t & 1 ? o.y1 = Math.max(0, Math.min(e.y1 + r, e.y2 - a.height)) : t & 4 && (o.y2 = Math.min(i.height, Math.max(e.y2 + r, e.y1 + a.height))), t & 8 ? o.x1 = Math.max(0, Math.min(e.x1 + n, e.x2 - a.width)) : t & 2 && (o.x2 = Math.min(i.width, Math.max(e.x2 + n, e.x1 + a.width))), o;
}
function _(e, t) {
	let n = Math.max(0, Math.min(t.width - 1, Math.round(Math.min(e.x1, e.x2)))), r = Math.max(0, Math.min(t.height - 1, Math.round(Math.min(e.y1, e.y2))));
	return {
		x1: n,
		y1: r,
		x2: Math.max(n + 1, Math.min(t.width, Math.round(Math.max(e.x1, e.x2)))),
		y2: Math.max(r + 1, Math.min(t.height, Math.round(Math.max(e.y1, e.y2))))
	};
}
function v(e, t) {
	return {
		width: (e.x2 - e.x1) * 25.4 / Math.max(1, t.x),
		height: (e.y2 - e.y1) * 25.4 / Math.max(1, t.y)
	};
}
function y(e, t, n) {
	let r = String(e.content_mode ?? "auto");
	if (r === "disabled") return "Content detection disabled: the whole page is kept.";
	if (r !== "manual" && e.content_found === !1) return "No content found: the whole page is kept.";
	if (!t) return "";
	let i = `${Math.round(t.x2 - t.x1)} × ${Math.round(t.y2 - t.y1)} px`;
	if (!n) return `Content ${i} (${r}).`;
	let a = v(t, n);
	return `Content ${a.width.toFixed(1)} × ${a.height.toFixed(1)} mm (${i}, ${r}).`;
}
var b = [
	["top", 1],
	["right", 2],
	["bottom", 4],
	["left", 8],
	["top-left", 9],
	["top-right", 3],
	["bottom-right", 6],
	["bottom-left", 12]
], x = class {
	editable;
	callbacks;
	element;
	box;
	source = {
		width: 1,
		height: 1
	};
	display = {
		width: 1,
		height: 1
	};
	drag = null;
	constructor(e, t, n, r) {
		if (this.editable = n, this.callbacks = r, this.box = t, this.element = document.createElement("div"), this.element.className = "st-content-box", this.element.dataset.contentBox = "", n) for (let [e, t] of b) {
			let n = document.createElement("div");
			n.className = `st-content-handle st-content-${e}`, n.dataset.contentHandle = e, n.dataset.capturesPointer = "true", n.title = "Drag lines or corners to resize the content box.", n.addEventListener("pointerdown", (e) => this.start(t, e)), this.element.append(n);
		}
		e.append(this.element);
	}
	isDragging() {
		return this.drag !== null;
	}
	current() {
		return { ...this.box };
	}
	setBox(e) {
		this.drag || (this.box = e, this.place());
	}
	layout(e, t) {
		this.source = e, this.display = t, this.place();
	}
	scale() {
		return {
			x: this.display.width / Math.max(1, this.source.width),
			y: this.display.height / Math.max(1, this.source.height)
		};
	}
	place() {
		let { x: e, y: t } = this.scale();
		this.element.style.left = `${this.box.x1 * e}px`, this.element.style.top = `${this.box.y1 * t}px`, this.element.style.width = `${Math.max(1, (this.box.x2 - this.box.x1) * e)}px`, this.element.style.height = `${Math.max(1, (this.box.y2 - this.box.y1) * t)}px`;
	}
	start(e, t) {
		if (t.button === 0) {
			t.preventDefault(), t.stopPropagation(), this.drag = {
				mask: e,
				pointer: t.pointerId,
				x: t.clientX,
				y: t.clientY,
				start: this.current()
			}, this.element.classList.add("dragging"), window.addEventListener("pointermove", this.onMove), window.addEventListener("pointerup", this.onEnd), window.addEventListener("pointercancel", this.onEnd);
			try {
				t.currentTarget?.setPointerCapture?.(t.pointerId);
			} catch {}
		}
	}
	onMove = (e) => this.move(e);
	onEnd = (e) => this.end(e);
	move(e) {
		let t = this.drag;
		if (!t || t.pointer !== e.pointerId) return;
		e.preventDefault();
		let { x: n, y: r } = this.scale(), i = (e.clientX - t.x) / n, a = (e.clientY - t.y) / r;
		this.box = g(t.start, t.mask, i, a, this.source, {
			width: 10 / n,
			height: 10 / r
		}), this.place(), this.callbacks.onPreview(this.current());
	}
	end(e) {
		let t = this.drag;
		t && t.pointer === e.pointerId && (this.drag = null, window.removeEventListener("pointermove", this.onMove), window.removeEventListener("pointerup", this.onEnd), window.removeEventListener("pointercancel", this.onEnd), this.element.classList.remove("dragging"), this.box = _(this.box, this.source), this.place(), this.callbacks.onCommit(this.current()));
	}
	remove() {
		window.removeEventListener("pointermove", this.onMove), window.removeEventListener("pointerup", this.onEnd), window.removeEventListener("pointercancel", this.onEnd), this.element.remove();
	}
};
// frontend/dewarp-view.ts
function S(e, t, n) {
	return e * n + 2 * e * n ** 2 + (10 - 12 * e - t) * n ** 3 + (2 * t + 14 * e - 15) * n ** 4 + (6 - 5 * e - t) * n ** 5;
}
function ee(e, t) {
	return e * t + 2 * e * t ** 2 - 2 * e * t ** 4 - e * t ** 5;
}
var C = class {
	points;
	tensions;
	constructor(e, t) {
		this.points = e.map(([e, t]) => [e, t]), this.tensions = t ?? this.points.map((e, t) => t === 0 || t === this.points.length - 1 ? 0 : 1);
	}
	get numSegments() {
		return Math.max(0, this.points.length - 1);
	}
	segmentPoint(e, t) {
		let n = this.points.length - 1, r = [
			Math.max(0, e - 1),
			e,
			e + 1,
			Math.min(e + 2, n)
		], i = this.tensions[e] ?? 0, a = this.tensions[e + 1] ?? 0, o = Math.max(i, 0), s = Math.max(a, 0), [c, l, u, d] = [
			-1,
			0,
			1,
			2
		], f = l + o, p = u + s, m = l - o, h = u - s, g = -.5 * Math.min(i, 0), _ = -.5 * Math.min(a, 0), v = [
			2 * (c - f) ** 2,
			2 * (l - p) ** 2,
			2 * (u - m) ** 2,
			2 * (d - h) ** 2
		], y = (t - f) / (c - f), b = (t - h) / (d - h), x = [
			t <= f ? S(g, v[0], y) : ee(g, y),
			S(_, v[1], (t - p) / (l - p)),
			S(g, v[2], (t - m) / (u - m)),
			t >= h ? S(_, v[3], b) : ee(_, b)
		], C = x[0] + x[1] + x[2] + x[3], w = 0, T = 0;
		return r.forEach((e, t) => {
			let n = x[t] / C;
			w += n * this.points[e][0], T += n * this.points[e][1];
		}), [w, T];
	}
	pointAt(e) {
		let t = this.numSegments;
		if (t <= 0) return this.points[0] ?? [0, 0];
		if (e >= 1) return this.segmentPoint(t - 1, 1);
		let n = e * t, r = Math.floor(n);
		return this.segmentPoint(r, n - r);
	}
	toPolyline(e = .2) {
		let t = this.numSegments, n = this.pointAt(0);
		if (t === 0) return [n];
		let r = this.pointAt(1), i = [n], a = e * e, o = (e, n, r, s, c) => {
			let l = s[0] - n[0], u = s[1] - n[1];
			if (l * l + u * u < 1e-6 || c > 40) return;
			let d = .5 * (e + r), f = Math.floor(d * t + .5) / t, p = (f - e) * (r - e) > 0 && (f - r) * (e - r) > 0;
			p && (d = f);
			let m = this.pointAt(d);
			if (!p) {
				let e = T([n, s], m);
				if ((m[0] - e[0]) ** 2 + (m[1] - e[1]) ** 2 <= a) return;
			}
			o(e, n, d, m, c + 1), i.push(m), o(d, m, r, s, c + 1);
		};
		return o(0, n, 1, r, 0), i.push(r), i;
	}
	moveControlPoint(e, t) {
		let n = this.points.length - 1;
		if (e > 0 && e < n) {
			this.points[e] = [t[0], t[1]];
			return;
		}
		let r = this.points[e === 0 ? n : 0], i = this.points[e], a = i[0] - r[0], o = i[1] - r[1];
		if (a * a + o * o > 1) {
			let e = t[0] - r[0], n = t[1] - r[1], i = a * a + o * o, s = (e * a + n * o) / i, c = (n * a - e * o) / i;
			this.points = this.points.map(([e, t]) => {
				let n = e - r[0], i = t - r[1];
				return [r[0] + s * n - c * i, r[1] + c * n + s * i];
			});
		} else this.points = this.points.map((e, i) => {
			let a = i / n;
			return [r[0] + (t[0] - r[0]) * a, r[1] + (t[1] - r[1]) * a];
		}), e === 0 && this.points.reverse();
	}
};
function w(e, t) {
	let [n, r] = e, i = r[0] - n[0], a = r[1] - n[1], o = i * i + a * a;
	return Math.abs(o) <= 2 ** -52 ? 0 : ((t[0] - n[0]) * i + (t[1] - n[1]) * a) / o;
}
function T(e, t) {
	let n = w(e, t), [r, i] = e;
	return [r[0] + (i[0] - r[0]) * n, r[1] + (i[1] - r[1]) * n];
}
function E(e, t) {
	let [[n, r], [i, a]] = e, [[o, s], [c, l]] = t, u = (n - i) * (s - l) - (r - a) * (o - c);
	if (u === 0) return null;
	let d = n * a - r * i, f = o * l - s * c;
	return [(d * (o - c) - (n - i) * f) / u, (d * (s - l) - (r - a) * f) / u];
}
function te(e) {
	let t = [], n = [];
	for (let [[r, i], [a, o]] of e) t.push([
		-r,
		-i,
		-1,
		0,
		0,
		0,
		a * r,
		a * i
	]), n.push(-a), t.push([
		0,
		0,
		0,
		-r,
		-i,
		-1,
		o * r,
		o * i
	]), n.push(-o);
	return [...D(t, n), 1];
}
function D(e, t) {
	let n = t.length, r = e.map((e, n) => [...e, t[n]]);
	for (let e = 0; e < n; e += 1) {
		let t = e;
		for (let i = e + 1; i < n; i += 1) Math.abs(r[i][e]) > Math.abs(r[t][e]) && (t = i);
		if (Math.abs(r[t][e]) < 1e-14) throw Error("degenerate model");
		[r[e], r[t]] = [r[t], r[e]];
		for (let t = 0; t < n; t += 1) {
			if (t === e) continue;
			let i = r[t][e] / r[e][e];
			for (let a = e; a <= n; a += 1) r[t][a] -= i * r[e][a];
		}
	}
	return r.map((e, t) => e[n] / e[t]);
}
function O(e, [t, n]) {
	let r = e[6] * t + e[7] * n + e[8];
	return [(e[0] * t + e[1] * n + e[2]) / r, (e[3] * t + e[4] * n + e[5]) / r];
}
function k(e) {
	let [t, n, r, i, a, o, s, c, l] = e, u = a * l - o * c, d = -(i * l - o * s), f = i * c - a * s, p = t * u + n * d + r * f;
	return [
		u / p,
		-(n * l - r * c) / p,
		(n * o - r * a) / p,
		d / p,
		(t * l - r * s) / p,
		-(t * o - r * i) / p,
		f / p,
		-(t * c - n * s) / p,
		(t * a - n * i) / p
	];
}
function ne(e) {
	let t = D(e.map(([e, t]) => [
		-e,
		-1,
		e * t
	]), e.map(([, e]) => -e));
	return [
		t[0],
		t[1],
		t[2]
	];
}
function re(e, t) {
	let [n, r] = t, i = -(r[1] - n[1]), a = r[0] - n[0], o = e.map(([e, t]) => (e - n[0]) * i + (t - n[1]) * a), s = o[0], c = o[o.length - 1];
	if (s * c > 0) return T(t, Math.abs(s) < Math.abs(c) ? e[0] : e[e.length - 1]);
	let l = 0, u = e.length - 1, d = o[l];
	for (; l + 1 < u;) {
		let e = l + u >> 1;
		o[e] * d <= 0 ? u = e : (l = e, d = o[e]);
	}
	let f = [e[l], e[l + 1]];
	return E(t, f) ?? [.5 * (f[0][0] + f[1][0]), .5 * (f[0][1] + f[1][1])];
}
function A(e, t, n) {
	if (t.length === 0) return 0;
	if (t.length === 1) return n[0];
	let r;
	if (e < t[0]) r = 0;
	else if (e > t[t.length - 1]) r = t.length - 2;
	else {
		let n = 0, i = t.length - 1;
		for (; n + 1 < i;) {
			let r = n + i >> 1;
			(e - t[r]) * (e - t[n]) <= 0 ? i = r : n = r;
		}
		r = n;
	}
	let i = t[r], a = t[r + 1];
	return n[r] + (e - i) * (n[r + 1] - n[r]) / (a - i);
}
var j = class {
	top;
	bottom;
	depth;
	pln2img;
	img2pln;
	straightLineY;
	xs = [];
	arcs = [];
	directrixArcLength;
	constructor(e, t, n) {
		if (this.top = e, this.bottom = t, this.depth = n, e.length < 2 || t.length < 2) throw Error("each curve needs two points");
		this.pln2img = te([
			[[0, 0], e[0]],
			[[1, 0], e[e.length - 1]],
			[[0, 1], t[0]],
			[[1, 1], t[t.length - 1]]
		]), this.img2pln = k(this.pln2img);
		let r = this.coupled();
		this.straightLineY = this.calcStraightLineY(r);
		let i = -Infinity, a = 0;
		for (let [e, t, n] of r) {
			if (n <= i) continue;
			let r = [e, t], o = w(r, O(this.pln2img, [n, 0])), s = w(r, O(this.pln2img, [n, 1])), c = Math.min(.5, Math.max(-.5, this.depth * (1 - (s - o)))), l = this.xs.length === 0 ? 0 : this.arcs[this.arcs.length - 1] + Math.hypot(n - this.xs[this.xs.length - 1], c - a);
			this.xs.push(n), this.arcs.push(l), a = c, i = n;
		}
		if (this.directrixArcLength = this.arcs.length >= 2 ? this.arcs[this.arcs.length - 1] : 0, this.arcs.length > 1 && this.directrixArcLength !== 0) {
			let e = 1 / this.directrixArcLength;
			this.arcs = this.arcs.map((t) => t * e);
		}
	}
	coupled() {
		let e = this.top, t = this.bottom, n = 0, r = 0, i = e[0], a = e[0], o = t[0], s = t[0], c = 0, l = 0, u = [], d = () => {
			n += 1, n !== e.length && (i = a, a = e[n], c = O(this.img2pln, a)[0]);
		}, f = () => {
			r += 1, r !== t.length && (o = s, s = t[r], l = O(this.img2pln, s)[0]);
		};
		for (;;) if (c < l && n !== e.length) {
			let e = O(this.img2pln, a), n = a, i = E([n, O(this.pln2img, [e[0], e[1] + 1])], [s, o]) ?? s;
			u.push([
				n,
				i,
				e[0]
			]), d(), r !== t.length && (s[0] - i[0]) ** 2 + (s[1] - i[1]) ** 2 < 1 && f();
		} else if (r !== t.length) {
			let t = O(this.img2pln, s), r = s, o = E([r, O(this.pln2img, [t[0], t[1] + 1])], [a, i]) ?? a;
			u.push([
				o,
				r,
				t[0]
			]), f(), n !== e.length && (a[0] - o[0]) ** 2 + (a[1] - o[1]) ** 2 < 1 && d();
		} else return u;
	}
	calcStraightLineY(e) {
		let t = 0, n = 0;
		for (let [r, i, a] of e) {
			let e = [r, i], o = w(e, O(this.pln2img, [a, 0])), s = w(e, O(this.pln2img, [a, 1])), c = o, l = 1 - s, u = Math.abs(c + l);
			if (u < .01) continue;
			let d = (s * c + o * l) / (c + l), f = [r[0] + (i[0] - r[0]) * d, r[1] + (i[1] - r[1]) * d];
			t += O(this.img2pln, f)[1] * u, n += u;
		}
		return n === 0 ? .5 : t / n;
	}
	generatrix(e) {
		let t = A(e, this.arcs, this.xs), n = [O(this.pln2img, [t, 0]), O(this.pln2img, [t, 1])], r = w(n, re(this.top, n)), i = w(n, re(this.bottom, n)), a = w(n, O(this.pln2img, [t, this.straightLineY])), o = this.straightLineY, s = Math.abs(o) < .05 || Math.abs(o - 1) < .05 ? [
			[0, r],
			[1, i],
			[.5, .5 * (r + i)]
		] : [
			[0, r],
			[1, i],
			[o, a]
		];
		return {
			p1: n[0],
			p2: n[1],
			h: ne(s)
		};
	}
	mapToWarped([e, t]) {
		let n = this.generatrix(e), [r, i, a] = n.h, o = (t * r + i) / (t * a + 1);
		return [n.p1[0] + (n.p2[0] - n.p1[0]) * o, n.p1[1] + (n.p2[1] - n.p1[1]) * o];
	}
};
function ie(e, t) {
	for (let n of [e, t]) {
		if (n.length < 2) return !1;
		let e = n[0], t = n[n.length - 1];
		if (e[0] === t[0] && e[1] === t[1]) return !1;
	}
	let n = [
		e[0],
		e[e.length - 1],
		t[t.length - 1],
		t[0]
	], r = n.map((e, t) => {
		let r = n[t + 3 & 3], i = n[t + 1 & 3], a = -(e[1] - r[1]), o = e[0] - r[0];
		return a * (i[0] - e[0]) + o * (i[1] - e[1]);
	}), i = Math.min(...r), a = Math.max(...r);
	return i * a <= 0 ? !1 : !(Math.abs(i) < .01 || Math.abs(a) < .01);
}
function ae(e, t = 30) {
	let n = [], r = Array.from({ length: t }, () => []);
	for (let i = 0; i < t; i += 1) {
		let a = i / (t - 1), o = e.generatrix(a), [s, c, l] = o.h, u = (e) => {
			let t = (e * s + c) / (e * l + 1);
			return [o.p1[0] + (o.p2[0] - o.p1[0]) * t, o.p1[1] + (o.p2[1] - o.p1[1]) * t];
		};
		n.push([u(0), u(1)]);
		for (let e = 0; e < t; e += 1) r[e].push(u(e / (t - 1)));
	}
	return {
		vertical: n,
		horizontal: r
	};
}
function oe(e, t) {
	return Math.min(e.numSegments, Math.floor(t * e.numSegments)) + 1;
}
function se(e, t, n = 200) {
	let r = {
		t: 0,
		dist: Infinity
	};
	for (let i = 0; i <= n; i += 1) {
		let a = i / n, o = e.pointAt(a), s = Math.hypot(o[0] - t[0], o[1] - t[1]);
		s < r.dist && (r = {
			t: a,
			dist: s
		});
	}
	return r;
}
function ce(e) {
	let t = String(e.dewarp_mode ?? "auto"), n = Number(e.depth_perception ?? 2), r = typeof e.dewarp_warning == "string" ? e.dewarp_warning : "";
	return t === "off" ? "Dewarping is off: the page is unchanged." : r || (e.dewarp_applied === !0 ? `Dewarped (${t}, depth perception ${n.toFixed(1)}).` : `No change (${t}).`);
}
var le = "http://www.w3.org/2000/svg";
function M(e, t = {}) {
	let n = document.createElementNS(le, e);
	for (let [e, r] of Object.entries(t)) n.setAttribute(e, String(r));
	return n;
}
function ue(e, t, n) {
	return e.map(([e, r], i) => `${i ? "L" : "M"} ${(e * t).toFixed(2)} ${(r * n).toFixed(2)}`).join(" ");
}
var N = class {
	top;
	bottom;
	editable;
	callbacks;
	element;
	grid;
	margins;
	points;
	hit;
	source = {
		width: 1,
		height: 1
	};
	display = {
		width: 1,
		height: 1
	};
	drag = null;
	hovered = null;
	depth = 2;
	constructor(e, t, n, r, i) {
		this.top = t, this.bottom = n, this.editable = r, this.callbacks = i, this.element = M("svg", { class: "st-dewarp-overlay" }), this.element.dataset.dewarpOverlay = "", this.margins = M("path", { class: "st-dewarp-margins" }), this.grid = M("path", { class: "st-dewarp-grid" }), this.points = M("g", { class: "st-dewarp-points" }), this.hit = M("path", { class: "st-dewarp-hit" }), this.element.append(this.margins, this.grid, this.hit, this.points), r && (this.hit.dataset.capturesPointer = "true", this.hit.dataset.dewarpCurve = "", this.element.addEventListener("pointerdown", (e) => this.onCurvePointerDown(e)), document.addEventListener("keydown", this.onKey)), e.append(this.element);
	}
	drawnShape = "";
	isDragging() {
		return this.drag !== null;
	}
	setCurves(e, t, n) {
		this.drag || (this.top = e, this.bottom = t, this.depth = n, this.draw());
	}
	layout(e, t) {
		this.source = e, this.display = t, this.element.setAttribute("width", String(t.width)), this.element.setAttribute("height", String(t.height)), this.draw();
	}
	scale() {
		return [this.display.width / Math.max(1, this.source.width), this.display.height / Math.max(1, this.source.height)];
	}
	draw() {
		let [e, t] = this.scale(), n = this.top.toPolyline(), r = this.bottom.toPolyline(), i = "";
		if (ie(n, r)) try {
			let a = ae(new j(n, r, this.depth));
			i = [...a.vertical, ...a.horizontal].map((n) => ue(n, e, t)).join(" ");
		} catch {
			i = "";
		}
		i ||= [
			n,
			r,
			[n[0], r[0]],
			[n[n.length - 1], r[r.length - 1]]
		].map((n) => ue(n, e, t)).join(" "), this.grid.setAttribute("d", i), this.margins.setAttribute("d", this.marginPath(n, r, e, t)), this.hit.setAttribute("d", `${ue(n, e, t)} ${ue(r, e, t)}`);
		let a = `${this.top.points.length}:${this.bottom.points.length}`, o = [...this.points.children];
		if (a === this.drawnShape && o.length > 0) {
			let n = 0;
			for (let r of [this.top, this.bottom]) for (let [i, a] of r.points) {
				let r = o[n++];
				r.setAttribute("cx", String(i * e)), r.setAttribute("cy", String(a * t));
			}
		} else this.drawnShape = a, this.points.replaceChildren(), [this.top, this.bottom].forEach((n, r) => {
			n.points.forEach(([i, a], o) => {
				let s = M("circle", {
					cx: i * e,
					cy: a * t,
					r: 5,
					class: "st-dewarp-point"
				});
				if (this.editable) {
					s.dataset.capturesPointer = "true", s.dataset.dewarpPoint = `${r}:${o}`;
					let e = M("title");
					e.textContent = o === 0 || o === n.points.length - 1 ? "This point can be dragged." : "Drag this point or delete it by pressing Del or D.", s.append(e), s.addEventListener("pointerdown", (e) => this.startDrag(r, o, e)), s.addEventListener("pointerenter", () => this.hovered = {
						curve: r,
						index: o
					}), s.addEventListener("pointerleave", () => this.hovered = null);
				}
				this.points.append(s);
			});
		});
	}
	marginPath(e, t, n, r) {
		let i = this.source.height, a = this.source.width, o = (e, t, n) => t[1] === e[1] ? e[0] : e[0] + (n - e[1]) * (t[0] - e[0]) / (t[1] - e[1]), s = e[0], c = t[0], l = e[e.length - 1], u = t[t.length - 1], d = [
			[0, 0],
			[o(s, c, 0), 0],
			[o(s, c, i), i],
			[0, i]
		], f = [
			[a, 0],
			[o(l, u, 0), 0],
			[o(l, u, i), i],
			[a, i]
		];
		return `${ue(d, n, r)} Z ${ue(f, n, r)} Z`;
	}
	toSource(e) {
		let t = this.element.getBoundingClientRect(), [n, r] = this.scale();
		return [(e.clientX - t.left) / n, (e.clientY - t.top) / r];
	}
	startDrag(e, t, n) {
		n.button === 0 && (n.preventDefault(), n.stopPropagation(), this.drag = {
			curve: e,
			index: t,
			pointer: n.pointerId
		}, window.addEventListener("pointermove", this.onMove), window.addEventListener("pointerup", this.onEnd), window.addEventListener("pointercancel", this.onEnd));
	}
	onCurvePointerDown(e) {
		if (e.button !== 0 || this.drag || e.target?.closest("[data-dewarp-point]")) return;
		let t = this.toSource(e), [n] = this.scale(), r = 8 / Math.max(n, 1e-6), i = [this.top, this.bottom].map((e, n) => ({
			curve: n,
			spline: e,
			...se(e, t)
		})).sort((e, t) => e.dist - t.dist)[0];
		if (i.dist > r) return;
		let a = oe(i.spline, i.t);
		i.spline.points.splice(a, 0, i.spline.pointAt(i.t)), i.spline.tensions.splice(a, 0, 1), this.draw(), this.startDrag(i.curve, a, e);
	}
	onMove = (e) => {
		let t = this.drag;
		t && t.pointer === e.pointerId && (e.preventDefault(), (t.curve === 0 ? this.top : this.bottom).moveControlPoint(t.index, this.toSource(e)), this.draw(), this.callbacks.onPreview(this.top, this.bottom));
	};
	onEnd = (e) => {
		let t = this.drag;
		t && t.pointer === e.pointerId && (this.drag = null, window.removeEventListener("pointermove", this.onMove), window.removeEventListener("pointerup", this.onEnd), window.removeEventListener("pointercancel", this.onEnd), this.callbacks.onCommit(this.top, this.bottom));
	};
	onKey = (e) => {
		if (e.key !== "Delete" && e.key !== "d" && e.key !== "D") return;
		let t = e.target;
		if (t instanceof Element && t.closest("input, select, textarea")) return;
		let n = this.hovered;
		if (!n || this.drag) return;
		let r = n.curve === 0 ? this.top : this.bottom;
		n.index <= 0 || n.index >= r.points.length - 1 || (r.points.splice(n.index, 1), r.tensions.splice(n.index, 1), this.hovered = null, e.preventDefault(), this.draw(), this.callbacks.onCommit(this.top, this.bottom));
	};
	remove() {
		window.removeEventListener("pointermove", this.onMove), window.removeEventListener("pointerup", this.onEnd), window.removeEventListener("pointercancel", this.onEnd), document.removeEventListener("keydown", this.onKey), this.element.remove();
	}
}, de = .1, fe = .05, pe = Math.PI / 180;
function me(e) {
	return Number.isFinite(e) ? Math.max(-45, Math.min(45, e)) : 0;
}
function P(e) {
	return Math.round(me(e) * 100) / 100 + 0;
}
function he(e, t, n) {
	let r = Math.cos(n * pe), i = Math.sin(n * pe);
	return [{
		x: e.x - r * t,
		y: e.y - i * t
	}, {
		x: e.x + r * t,
		y: e.y + i * t
	}];
}
function ge(e, t, n, r) {
	if (r <= 0) return 0;
	let i = Math.max(-r, Math.min(r, t.y - n.y)), a = Math.asin(i / r) * 180 / Math.PI;
	return e === 0 && (a = -a), me(a) + 0;
}
function _e(e, t) {
	if (!t.ctrlKey) return null;
	let n = t.deltaY || t.deltaX || 0;
	if (n === 0) return null;
	let r = t.shiftKey ? fe : de;
	return P(e + (n < 0 ? -r : r));
}
function ve(e, t, n, r) {
	if (e === "all") return null;
	if (e === "selected") return t.filter((e) => r.has(e));
	if (!n) return [];
	let i = t.indexOf(n);
	return e === "this" || i < 0 ? [n] : e === "following" ? t.slice(i) : t.filter((e, t) => (t - i) % 2 == 0);
}
function ye(e) {
	return `${Math.abs(e).toFixed(2)}°`;
}
function be(e) {
	let t = P(e);
	return t === 0 ? "Angle 0.00°, no change" : `Angle ${ye(t)} ${t > 0 ? "clockwise" : "counter-clockwise"}`;
}
function xe(e) {
	let t = Number(e.angle);
	if (!Number.isFinite(t)) return "";
	let n = e.deskew_mode === "manual" ? "manual" : "auto", r = be(t);
	if (n === "manual") return `${r} (manual).`;
	let i = Number(e.confidence), a = Number(e.detected_angle);
	if (e.confidence_good === !1 && Number.isFinite(i)) {
		let e = Number.isFinite(a) ? `; best guess ${ye(a)}` : "";
		return `${r}: detection confidence ${i.toFixed(1)} is below 2.0${e}.`;
	}
	return Number.isFinite(i) ? `${r} (auto, confidence ${i.toFixed(1)}).` : `${r} (auto).`;
}
var Se = "http://www.w3.org/2000/svg";
function F(e, t = {}) {
	let n = document.createElementNS(Se, e);
	for (let [e, r] of Object.entries(t)) n.setAttribute(e, String(r));
	return n;
}
var Ce = class {
	callbacks;
	element;
	grid;
	pattern;
	cross;
	arcs;
	handles;
	centre = {
		x: 0,
		y: 0
	};
	radius = 0;
	angle = 0;
	dragging = null;
	keepGrid = !0;
	constructor(e, t) {
		this.callbacks = t;
		let n = `deskew-grid-${Math.random().toString(36).slice(2)}`;
		this.element = F("svg", {
			class: "st-deskew-overlay",
			"aria-hidden": "true"
		});
		let r = F("defs");
		this.pattern = F("pattern", {
			id: n,
			width: 20,
			height: 20,
			patternUnits: "userSpaceOnUse"
		}), this.pattern.append(F("path", {
			d: "M 20 0 L 0 0 0 20",
			class: "st-deskew-grid-line"
		})), r.append(this.pattern), this.grid = F("rect", {
			width: "100%",
			height: "100%",
			fill: `url(#${n})`
		}), this.grid.classList.add("st-deskew-grid"), this.cross = F("path", { class: "st-deskew-cross" }), this.arcs = F("path", { class: "st-deskew-arcs" });
		let i = (e) => {
			let t = F("circle", {
				r: 9,
				class: "st-deskew-handle"
			});
			t.dataset.capturesPointer = "true", t.dataset.deskewHandle = String(e);
			let n = F("title");
			return n.textContent = "Drag this handle to rotate the image.", t.append(n), t.addEventListener("pointerdown", (t) => this.startDrag(e, t)), t.addEventListener("pointermove", (e) => this.moveDrag(e)), t.addEventListener("pointerup", (e) => this.endDrag(e)), t.addEventListener("pointercancel", (e) => this.endDrag(e)), t;
		};
		this.handles = [i(0), i(1)], this.element.append(r, this.grid, this.cross, this.arcs, ...this.handles), e.append(this.element);
	}
	setKeepGrid(e) {
		this.keepGrid = e, this.updateGridVisibility();
	}
	isDragging() {
		return this.dragging !== null;
	}
	layout(e, t, n) {
		this.centre = e, this.radius = Math.max(0, t), this.angle = n;
		let r = this.element.clientWidth || this.element.parentElement?.clientWidth || 0, i = this.element.clientHeight || this.element.parentElement?.clientHeight || 0, a = (e.x % 20 + 20) % 20, o = (e.y % 20 + 20) % 20;
		this.pattern.setAttribute("x", String(a)), this.pattern.setAttribute("y", String(o)), this.cross.setAttribute("d", `M 0 ${e.y} H ${r} M ${e.x} 0 V ${i}`);
		let s = this.radius, c = Math.sin(45 * pe) * s, l = Math.cos(45 * pe) * s;
		this.arcs.setAttribute("d", [`M ${e.x + l} ${e.y - c} A ${s} ${s} 0 0 1 ${e.x + l} ${e.y + c}`, `M ${e.x - l} ${e.y + c} A ${s} ${s} 0 0 1 ${e.x - l} ${e.y - c}`].join(" ")), this.placeHandles(), this.updateGridVisibility();
	}
	setAngle(e) {
		this.angle = e, this.placeHandles();
	}
	flashGrid() {
		this.element.classList.add("rotating"), window.clearTimeout(this.flashTimer), this.flashTimer = window.setTimeout(() => {
			this.element.classList.remove("rotating"), this.updateGridVisibility();
		}, 900), this.updateGridVisibility();
	}
	flashTimer = 0;
	updateGridVisibility() {
		let e = this.dragging !== null || this.element.classList.contains("rotating"), t = this.keepGrid || e;
		this.grid.style.display = t ? "" : "none", this.cross.style.display = t ? "" : "none";
	}
	placeHandles() {
		he(this.centre, this.radius, this.angle).forEach((e, t) => {
			this.handles[t]?.setAttribute("cx", String(e.x)), this.handles[t]?.setAttribute("cy", String(e.y));
		});
	}
	pointerPoint(e) {
		let t = this.element.getBoundingClientRect();
		return {
			x: e.clientX - t.left,
			y: e.clientY - t.top
		};
	}
	startDrag(e, t) {
		t.button === 0 && (t.preventDefault(), t.stopPropagation(), this.dragging = {
			index: e,
			pointer: t.pointerId
		}, this.handles[e].setPointerCapture?.(t.pointerId), this.element.classList.add("dragging"), this.updateGridVisibility());
	}
	moveDrag(e) {
		if (!this.dragging || this.dragging.pointer !== e.pointerId) return;
		e.preventDefault();
		let t = ge(this.dragging.index, this.pointerPoint(e), this.centre, this.radius);
		t !== this.angle && (this.setAngle(t), this.callbacks.onPreview(t));
	}
	endDrag(e) {
		this.dragging && this.dragging.pointer === e.pointerId && (this.dragging = null, this.element.classList.remove("dragging"), this.updateGridVisibility(), this.callbacks.onCommit(P(this.angle)));
	}
	remove() {
		window.clearTimeout(this.flashTimer), this.element.remove();
	}
}, we = [
	"top",
	"bottom",
	"left",
	"right"
], I = {
	left_mm: 10,
	top_mm: 5,
	right_mm: 10,
	bottom_mm: 5,
	align_x: "center",
	align_y: "top",
	align_with_others: !0
}, Te = 25.4;
function L(e) {
	let t = typeof e == "number" ? e : typeof e == "string" ? Number(e) : NaN;
	return Number.isFinite(t) ? t : null;
}
function Ee(e) {
	return typeof e == "object" && !!e && !Array.isArray(e);
}
function De(e) {
	return Math.min(500, Math.max(0, e));
}
function Oe(e) {
	return e === "left" || e === "right" ? e : e === "center" ? "center" : I.align_x;
}
function ke(e) {
	return e === "top" || e === "bottom" ? e : e === "center" ? "center" : I.align_y;
}
function Ae(e) {
	let t = e.dpi;
	if (Array.isArray(t) && t.length >= 2) {
		let e = L(t[0]), n = L(t[1]);
		if (e && n && e > 0 && n > 0) return {
			x: e,
			y: n
		};
	}
	return {
		x: 300,
		y: 300
	};
}
function je(e) {
	let t = {
		left: "left_mm",
		top: "top_mm",
		right: "right_mm",
		bottom: "bottom_mm"
	}, n = {
		left: I.left_mm,
		top: I.top_mm,
		right: I.right_mm,
		bottom: I.bottom_mm
	}, r = Object.values(t).some((t) => t in e), i = Ee(e.hard_margins_mm) ? e.hard_margins_mm : null, a = [
		"margin",
		"left",
		"top",
		"right",
		"bottom"
	].some((t) => t in e);
	for (let o of Object.keys(t)) if (r) {
		let r = L(e[t[o]]);
		r !== null && (n[o] = De(r));
	} else if (i) {
		let e = L(i[o]);
		e !== null && (n[o] = De(e));
	} else if (a) {
		let t = Ae(e), r = L(e[o]) ?? L(e.margin) ?? 0, i = o === "left" || o === "right" ? t.x : t.y;
		n[o] = De(Math.round(Math.max(0, r) * Te / i * 10) / 10);
	}
	return {
		left_mm: n.left,
		top_mm: n.top,
		right_mm: n.right,
		bottom_mm: n.bottom,
		align_x: Oe(e.align_x),
		align_y: ke(e.align_y),
		align_with_others: e.align_with_others !== !1
	};
}
function Me(e) {
	if (!Ee(e)) return null;
	let t = L(e.width), n = L(e.height);
	return t !== null && n !== null ? {
		width: t,
		height: n
	} : null;
}
var Ne = (e) => e.toFixed(1);
function Pe(e) {
	let t = Me(e.page_size_mm);
	if (!t) return "";
	let n = `${Ne(t.width)} × ${Ne(t.height)} mm`;
	if (e.align_with_others === !1) return `Page ${n}: not aligned with other pages, it keeps its own size.`;
	let r = Me(e.aggregate_size_mm), i = Ee(e.soft_margins_mm) ? e.soft_margins_mm : {}, a = [
		"left",
		"top",
		"right",
		"bottom"
	].map((e) => `${e} ${Ne(L(i[e]) ?? 0)}`).join(", "), o = e.content_found === !1 ? " No content: the page is centred." : "";
	return `Page ${n}` + (r ? ` (common size ${Ne(r.width)} × ${Ne(r.height)} mm)` : "") + `. Soft margins ${a} mm.${o}`;
}
function Fe(e) {
	return e === "top" ? "bottom" : e === "bottom" ? "top" : e === "left" ? "right" : "left";
}
// frontend/pan-zoom.ts
var Ie = .05, Le = 1.2;
function Re(e) {
	return Number.isFinite(e) ? Math.min(8, Math.max(Ie, e)) : 1;
}
function ze(e, t, n = 16) {
	let r = Math.max(1, t.width), i = Math.max(1, t.height), a = Re(Math.min(Math.max(1, e.width - 2 * n) / r, Math.max(1, e.height - 2 * n) / i));
	return {
		zoom: a,
		x: (e.width - r * a) / 2,
		y: (e.height - i * a) / 2
	};
}
function Be(e, t, n) {
	let r = Re(e.zoom * t), i = r / e.zoom;
	return {
		zoom: r,
		x: n.x - (n.x - e.x) * i,
		y: n.y - (n.y - e.y) * i
	};
}
function Ve(e, t = 0) {
	return Le ** (-(t === 1 ? e * 33 : t === 2 ? e * 400 : e) / 100);
}
function He(e, t, n) {
	return {
		zoom: e.zoom,
		x: e.x + t,
		y: e.y + n
	};
}
// frontend/scantailor-workspace.ts
var R = [
	"deskew",
	"select_content",
	"dewarping",
	"margins",
	"output"
], z = {
	deskew: "Deskew",
	select_content: "Select content",
	margins: "Margins",
	dewarping: "Dewarping",
	output: "Output / binarization"
}, Ue = {
	deskew: ["manual_angle"],
	select_content: ["content_mode", "content_box"],
	margins: [
		"margins_mode",
		"left_mm",
		"top_mm",
		"right_mm",
		"bottom_mm",
		"align_x",
		"align_y",
		"align_with_others"
	],
	dewarping: [
		"dewarp_mode",
		"depth_perception",
		"top_curve",
		"bottom_curve"
	],
	output: [
		"output_mode",
		"keep_color",
		"invert",
		"zone_mode",
		"zones",
		"text_box",
		"picture_box",
		"text_algorithm",
		"picture_algorithm",
		"window_size",
		"k",
		"r"
	]
};
function B(e, t, n) {
	return Math.min(n, Math.max(t, e));
}
function We(e, t, n) {
	let r = n.width / Math.max(1, t.width), i = n.height / Math.max(1, t.height);
	return {
		x1: e.x1 * r,
		y1: e.y1 * i,
		x2: e.x2 * r,
		y2: e.y2 * i
	};
}
function Ge(e, t, n) {
	let r = t.width / Math.max(1, n.width), i = t.height / Math.max(1, n.height);
	return {
		x1: Math.round(B(e.x1 * r, 0, t.width)),
		y1: Math.round(B(e.y1 * i, 0, t.height)),
		x2: Math.round(B(e.x2 * r, 0, t.width)),
		y2: Math.round(B(e.y2 * i, 0, t.height))
	};
}
function Ke(e, t) {
	let n = {};
	for (let r of Ue[e]) r in t && (e !== "output" || r !== "zones" || t.zone_mode === "manual") && (n[r] = structuredClone(t[r]));
	if (e === "margins") {
		let e = je(t);
		return {
			...typeof t.margins_mode == "string" ? { margins_mode: t.margins_mode } : {},
			...e
		};
	}
	return n;
}
function qe(e) {
	let t = e.zone_mode === "manual" && Array.isArray(e.zones) ? e.zones : e.resolved_zones;
	return Array.isArray(t) ? t.slice(0, 64).flatMap((e) => !yt(e) || e.kind !== "text" && e.kind !== "picture" || !Array.isArray(e.box) || e.box.length !== 4 || !e.box.every((e) => typeof e == "number" && Number.isFinite(e)) ? [] : [{
		kind: e.kind,
		box: [...e.box]
	}]) : ["text", "picture"].flatMap((t) => {
		let n = e[`${t}_box`];
		if (!(Array.isArray(n) && n.length === 4) && !yt(n)) return [];
		let r = _t(n, {
			x1: 0,
			y1: 0,
			x2: 1,
			y2: 1
		});
		return [{
			kind: t,
			box: [
				r.x1,
				r.y1,
				r.x2,
				r.y2
			]
		}];
	});
}
function V(e) {
	return [e.dewarp_warning, e.output_warning].filter((e) => typeof e == "string" && e.trim() !== "").join(" · ");
}
function H(e) {
	let t = R.indexOf(e);
	return t > 0 ? R[t - 1] ?? null : null;
}
function U(e, t) {
	let { status: n } = t, r = t.body.trim().toLowerCase();
	if (n === null) return "Could not load the preview because of a network error. Check your connection and try again.";
	if (Ze(t)) return "The page image is still being prepared. It will appear automatically.";
	if (n === 404 && r === "input preview not ready") {
		let t = H(e);
		return t === null ? "The scanned page image is not available yet. Wait for the upload to finish processing, then reopen this page." : `Waiting for ${z[t]}: its result is not ready yet.`;
	}
	return n === 404 && r === "preview not ready" ? "The preview for this stage has not been generated yet. It will appear once the worker finishes." : n === 404 && r === "preview artifact missing" ? "The preview file is missing from storage. Change a setting of this stage to run it again." : n === 503 ? "Scan storage is temporarily unavailable. Try again shortly." : n === 401 || n === 403 ? "You are not allowed to view this preview. Your session may have expired; sign in again." : n >= 200 && n < 300 ? "The preview was downloaded but could not be displayed as an image." : `Could not load the preview (HTTP ${n}).`;
}
var Je = 10, Ye = 1e3, Xe = 8e3, W = "The page image is taking longer than expected to prepare. Reopen this page later.", G = {
	attempts: 0,
	reloadedAfterOk: !1
};
function Ze(e) {
	return e.status === 503 && e.body.trim().toLowerCase() === "input preview not ready";
}
function Qe(e, t) {
	return e.status !== null && e.status >= 200 && e.status < 300 ? t.reloadedAfterOk ? null : {
		delay: 0,
		state: {
			...t,
			reloadedAfterOk: !0
		}
	} : !Ze(e) || t.attempts >= 10 ? null : {
		delay: Math.min(Ye * 2 ** t.attempts, Xe),
		state: {
			...t,
			attempts: t.attempts + 1
		}
	};
}
function $e(e, t) {
	return `${e}${e.includes("?") ? "&" : "?"}retry=${t}`;
}
function K(e) {
	return e.status === "failed" ? e.last_error || "Stage failed" : "";
}
function et(e) {
	return !e || e.status !== "success" || !e.output_sha256 ? "" : `${e.revision}-${e.output_sha256.slice(0, 16)}`;
}
function tt(e, t, n = "") {
	return e.replace("__PAGE__", t).replace("__STAGE__", n);
}
function nt(e, t) {
	let n;
	for (let r of R) {
		let i = e.stages.find((e) => e.stage === r);
		if (!i || !q(e, r).current || !et(i) || (n = i, r === t)) break;
	}
	return n;
}
function rt(e, t) {
	let n = e.stages.find((e) => e.stage === t)?.parameters ?? {};
	return {
		width: Y(t === "select_content" ? n.content_area_width : n.output_width),
		height: Y(t === "select_content" ? n.content_area_height : n.output_height)
	};
}
function it(e) {
	return e.status;
}
function at(e, t) {
	if (e.status !== "success" || e.settings_applied !== !0) return !1;
	let n = e.stage === R[0] ? e.input_sha256 ?? "" : t?.status === "success" ? t.output_sha256 ?? "" : "";
	return e.input_basis_sha256 && (n = t?.status === "success" && t.output_sha256 === e.input_basis_sha256 ? e.input_sha256 ?? "" : ""), n === (e.applied_input_sha256 ?? "");
}
function q(e, t) {
	let n = !0, r = null, i;
	for (let a of R) {
		let o = e.stages.find((e) => e.stage === a), s = n && o !== void 0 && at(o, i);
		if (a === t) {
			let e = "";
			return o?.status === "success" && !s && (e = o.settings_applied === !0 ? n && o.common_size_changed === !0 && i?.output_sha256 === o.input_basis_sha256 ? "common page size changed" : "input changed" : "settings changed, not applied"), {
				current: s,
				waitingFor: r,
				staleReason: e
			};
		}
		!s && r === null && (r = a), n = s, i = o;
	}
	return {
		current: !1,
		waitingFor: r,
		staleReason: ""
	};
}
function ot(e, t, n = !1, r = !1) {
	let i = K(e);
	if (i) return {
		text: `Failed: ${i}`,
		tone: "error"
	};
	if (n) return {
		text: st,
		tone: "info"
	};
	let a = `Revision ${e.revision}`;
	return e.status === "queued" || e.status === "running" ? {
		text: r ? lt : ct,
		tone: "info"
	} : t?.waitingFor ? {
		text: `Waiting for ${z[t.waitingFor]}.`,
		tone: "info"
	} : e.status === "success" ? t?.staleReason ? {
		text: `${a} · stale: ${t.staleReason}`,
		tone: "info"
	} : {
		text: `${a} · success`,
		tone: "info"
	} : {
		text: `${a} · not run yet`,
		tone: "info"
	};
}
var st = "Settings not applied yet.", ct = "Running…", lt = "Restarting with new settings…";
function ut(e) {
	return e ? lt : "Saving…";
}
var dt = 600;
function ft(e) {
	return JSON.stringify(e, (e, t) => yt(t) ? Object.fromEntries(Object.entries(t).sort(([e], [t]) => e.localeCompare(t))) : t);
}
function pt(e) {
	if (typeof e != "string") return null;
	let t = /^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(.*)$/.exec(e);
	if (!t) return null;
	let n = Date.parse(`${t[1]}${t[3] ?? ""}`);
	return Number.isNaN(n) ? null : n * 1e3 + Number((t[2] ?? "").padEnd(6, "0"));
}
var mt = {
	unknown: 0,
	queued: 1,
	running: 2,
	success: 3,
	failed: 3
};
function ht(e, t) {
	if (e.revision !== t.revision) return e.revision < t.revision;
	let n = pt(e.updated_at), r = pt(t.updated_at);
	return n !== null && r !== null && n !== r ? n < r : (mt[e.status] ?? 0) < (mt[t.status] ?? 0);
}
function gt(e, t, n = !1) {
	let r = K(e);
	return r ? `Failed: ${r}` : n ? `${st} The result appears once they are saved.` : e.status === "queued" ? "Queued: the result appears here once the worker has run this stage." : e.status === "running" ? "Running: the result appears here when it finishes." : t.waitingFor ? `Waiting for ${z[t.waitingFor]}. This stage runs when it is done.` : e.status === "success" ? t.current ? e.preview_storage_identifier || e.output_storage_identifier ? "" : "This run produced no result preview." : `Stale: ${t.staleReason || "input changed"}. It runs again by itself.` : "Not run yet. The result appears here once this stage has run.";
}
function _t(e, t) {
	if (Array.isArray(e) && e.length >= 4) return vt({
		x1: J(e[0], t.x1),
		y1: J(e[1], t.y1),
		x2: J(e[2], t.x2),
		y2: J(e[3], t.y2)
	});
	if (yt(e)) {
		let n = J(e.x1, t.x1), r = J(e.y1, t.y1), i = J(e.width, t.x2 - t.x1), a = J(e.height, t.y2 - t.y1);
		return vt({
			x1: n,
			y1: r,
			x2: J(e.x2, n + i),
			y2: J(e.y2, r + a)
		});
	}
	return t;
}
function vt(e) {
	return {
		x1: Math.min(e.x1, e.x2),
		y1: Math.min(e.y1, e.y2),
		x2: Math.max(e.x1, e.x2),
		y2: Math.max(e.y1, e.y2)
	};
}
function J(e, t) {
	let n = typeof e == "number" ? e : Number(e);
	return Number.isFinite(n) ? n : t;
}
function Y(e) {
	let t = J(e, 0);
	return t > 0 ? t : null;
}
function yt(e) {
	return typeof e == "object" && !!e && !Array.isArray(e);
}
function bt(e) {
	return R.includes(e);
}
function X(e, t) {
	return e.stages.find((e) => e.stage === t);
}
function Z() {
	return document.querySelector("input[name='csrfmiddlewaretoken']")?.value ?? "";
}
function Q(e, t) {
	let n = e.querySelector(t);
	if (!n) throw Error(`ScanTailor workspace element missing: ${t}`);
	return n;
}
var xt = "scantailor.deskew.keepGrid";
function St() {
	try {
		return window.localStorage.getItem(xt) !== "false";
	} catch {
		return !0;
	}
}
function Ct(e) {
	try {
		window.localStorage.setItem(xt, String(e));
	} catch {}
}
var wt = class {
	root;
	pages = /* @__PURE__ */ new Map();
	selectedPages = /* @__PURE__ */ new Set();
	editable;
	controls;
	image;
	geometryLayer;
	message;
	liveStatus;
	title;
	revertButton;
	batchButton;
	batchScope;
	preserveExceptions;
	selectionCount;
	inputButton;
	resultButton;
	warnings;
	previewMode = "input";
	panes;
	resultPane;
	resultViewport;
	resultSurface;
	resultImage;
	resultStatus;
	resultUrl = "";
	resultKey = "";
	activePageId = null;
	activeStage = R[0];
	draft = {};
	appliedDraft = "";
	interactions = [];
	eventSource = null;
	pollingTimer = null;
	pollCursor = null;
	viewport;
	surface;
	zoomLevel;
	statusHint;
	sortSelect;
	keepInView;
	strip;
	naturalOrder;
	view = {
		zoom: 1,
		x: 0,
		y: 0
	};
	fitPending = !0;
	selectionAnchor = null;
	previewUrl = "";
	previewRetry = G;
	previewRetryTimer = null;
	deskewOverlay = null;
	contentOverlay = null;
	dewarpOverlay = null;
	commitTimer = null;
	marginLinks = {
		horizontal: null,
		vertical: null
	};
	saving = !1;
	saveQueued = !1;
	leftDrafts = [];
	revertAfterSave = !1;
	restartingRevision = null;
	ensuring = Promise.resolve();
	constructor(e, t) {
		this.root = e, this.editable = e.dataset.editable === "true", this.controls = Q(e, "[data-stage-controls]"), this.image = Q(e, "[data-editor-image]"), this.geometryLayer = Q(e, "[data-geometry-layer]"), this.message = Q(e, "[data-editor-message]"), this.liveStatus = Q(e, "[data-live-status]"), this.title = Q(e, "[data-editor-title]"), this.revertButton = e.querySelector("[data-revert-stage]"), this.batchButton = Q(e, "[data-batch-stage]"), this.batchScope = Q(e, "[data-batch-scope]"), this.preserveExceptions = Q(e, "[data-preserve-exceptions]"), this.selectionCount = Q(e, "[data-selection-count]"), this.inputButton = Q(e, "[data-preview-input]"), this.resultButton = Q(e, "[data-preview-result]"), this.warnings = Q(e, "[data-stage-warnings]");
		for (let e of t) this.pages.set(e.id, e);
		this.naturalOrder = t.map((e) => e.id), this.viewport = e.querySelector("[data-viewport]"), this.surface = e.querySelector("[data-surface]"), this.panes = e.querySelector("[data-panes]"), this.resultPane = e.querySelector("[data-result-pane]"), this.resultViewport = e.querySelector("[data-result-viewport]"), this.resultSurface = e.querySelector("[data-result-surface]"), this.resultImage = e.querySelector("[data-result-image]"), this.resultStatus = e.querySelector("[data-result-status]"), this.zoomLevel = e.querySelector("[data-zoom-level]"), this.statusHint = e.querySelector("[data-status-hint]"), this.sortSelect = e.querySelector("[data-strip-sort]"), this.keepInView = e.querySelector("[data-keep-in-view]");
		let n = e.querySelector("[data-strip-scroll]");
		this.strip = n ? new h(n, { onClick: (e, t) => this.clickPage(e, t) }) : null;
	}
	url(e, t = "", n = "") {
		return tt(this.root.dataset[e] ?? "", t, n);
	}
	start() {
		this.bindWorkspaceControls();
		let e = new URLSearchParams(window.location.search), t = e.get("page") ?? "", n = e.get("stage") ?? "", r = this.pages.get(t) ?? this.pages.values().next().value;
		r ? this.activate(r.id, bt(n) ? n : R[0]) : this.disableEditor("Upload a page to begin editing."), this.startLiveUpdates();
	}
	bindWorkspaceControls() {
		this.inputButton.addEventListener("click", () => this.showPreview("input")), this.resultButton.addEventListener("click", () => this.showPreview("result")), this.root.querySelectorAll("[data-stage-select]").forEach((e) => {
			e.addEventListener("click", () => {
				let t = e.dataset.stageSelect ?? "";
				if (!bt(t)) return;
				let n = this.activePageId ?? this.pages.keys().next().value;
				typeof n == "string" && this.activate(n, t);
			});
		}), this.root.querySelectorAll("[data-stage-run-all]").forEach((e) => {
			e.addEventListener("click", () => {
				let t = e.dataset.stageRunAll ?? "";
				bt(t) && this.runStageForAllPages(t, e);
			});
		}), this.revertButton?.addEventListener("click", () => this.revertDraft()), this.controls.addEventListener("input", () => this.draftEdited()), this.controls.addEventListener("change", () => this.draftEdited()), this.batchButton.addEventListener("click", () => void this.applyBatch()), this.bindCanvas(), this.bindStrip(), this.batchScope.addEventListener("change", () => this.updateSelectionCount()), this.image.addEventListener("load", () => {
			this.fitPending ? (this.fitPending = !1, this.fitToPane()) : this.applyView(), this.renderGeometry(), this.previewRetry !== G && (this.previewRetry = G, this.showStatus());
		}), window.addEventListener("resize", () => {
			this.fitToPane();
		}), this.image.addEventListener("error", () => {
			this.clearGeometry(), this.explainPreviewFailure(this.image.src, this.activeStage);
		}), this.resultImage?.addEventListener("load", () => {
			this.fitPending && !this.image.naturalWidth ? (this.fitPending = !1, this.fitToPane()) : this.applyResultView();
		}), this.resultImage?.addEventListener("error", () => {
			this.resultImage?.getAttribute("src") && (this.resultImage.hidden = !0, this.setResultStatus("The result preview could not be loaded.", !1));
		}), this.updateSelectionCount();
	}
	async explainPreviewFailure(e, t) {
		if (!e) return;
		this.previewRetry === G && !this.settingsNotApplied() && this.setMessage("Loading the preview failed; checking why…", "error");
		let n;
		try {
			let t = await fetch(e, { credentials: "same-origin" });
			t.ok && t.body?.cancel().catch(() => void 0);
			let r = t.ok ? "" : await t.text();
			n = {
				status: t.status,
				body: r
			};
		} catch {
			n = {
				status: null,
				body: ""
			};
		}
		if (this.image.src !== e || this.activeStage !== t) return;
		let r = Qe(n, this.previewRetry);
		r ? (this.previewRetry = r.state, r.delay > 0 && this.showInputFailure(t, n, U(t, n), "info"), this.schedulePreviewReload(r.delay)) : this.showInputFailure(t, n, Ze(n) ? W : U(t, n), "error");
	}
	showInputFailure(e, t, n, r) {
		if (t.status === 404 && t.body.trim().toLowerCase() === "input preview not ready") {
			if (this.savePending() || this.settingsNotApplied()) return;
			let t = this.activeContext()?.page, n = t ? q(t, e).waitingFor : null;
			if (n) {
				this.setMessage(`Waiting for ${z[n]}: its result is not ready yet.`, r);
				return;
			}
		}
		this.setMessage(n, r);
	}
	schedulePreviewReload(e) {
		this.cancelPreviewReload();
		let t = this.previewUrl, n = this.previewRetry.attempts + +!!this.previewRetry.reloadedAfterOk;
		this.previewRetryTimer = window.setTimeout(() => {
			this.previewRetryTimer = null, t && this.previewUrl === t && (this.image.src = $e(t, n));
		}, e);
	}
	cancelPreviewReload() {
		this.previewRetryTimer !== null && window.clearTimeout(this.previewRetryTimer), this.previewRetryTimer = null;
	}
	activate(e, t) {
		let n = this.pages.get(e), r = n ? X(n, t) : void 0;
		if (!n || !r) return;
		(this.commitTimer !== null || this.saveQueued) && this.leaveDraft();
		let i = t !== this.activeStage;
		(e !== this.activePageId || i) && (this.fitPending = !0), this.activePageId = e, this.activeStage = t, this.selectedPages.has(e) || (this.selectedPages.clear(), this.selectedPages.add(e), this.selectionAnchor = e), this.draft = Ke(t, r.parameters ?? {});
		let a = this.root.querySelector(`[data-stage-select="${t}"]`)?.dataset.hint;
		this.statusHint && (this.statusHint.textContent = a ?? ""), i && this.refreshStrip(), this.strip?.setActive(e, new Set(this.selectedPages), this.keepInView?.checked ?? !0), this.updateSelectionCount(), this.title.textContent = `Page ${n.logical_sequence_number} · ${z[t]}`, this.markActiveControls(), this.restartingRevision = null, this.renderControls(), this.appliedDraft = ft(this.draft), this.showPreview(this.previewMode), this.revertButton && (this.revertButton.disabled = !this.editable), this.showStatus(), this.ensuring = this.ensureActiveStage();
	}
	async ensureActiveStage() {
		let e = this.activeContext();
		if (!e || !this.editable) return;
		let t = this.url("ensureUrlTemplate", e.page.id, e.state.stage);
		if (t) try {
			let e = await fetch(t, {
				method: "POST",
				credentials: "same-origin",
				headers: { "X-CSRFToken": Z() }
			});
			if (e.status === 507) {
				this.setMessage(await e.text(), "error");
				return;
			}
			if (!e.ok) return;
			let n = await e.json();
			this.mergeStage(n.stage);
		} catch {}
	}
	revertDraft() {
		this.commitTimer !== null && window.clearTimeout(this.commitTimer), this.commitTimer = null, this.saveQueued = !1, this.saving ? this.revertAfterSave = !0 : this.activePageId && this.activate(this.activePageId, this.activeStage);
	}
	activeSnapshot() {
		return this.activePageId ? {
			pageId: this.activePageId,
			stage: this.activeStage,
			draft: structuredClone(this.draft),
			sent: ft(this.draft)
		} : null;
	}
	leaveDraft() {
		this.commitTimer !== null && window.clearTimeout(this.commitTimer), this.commitTimer = null, this.saveQueued = !1;
		let e = this.settingsNotApplied() ? this.activeSnapshot() : null;
		e && this.editable && (this.leftDrafts.push(e), this.saving || this.commitDraft());
	}
	settingsNotApplied() {
		return ft(this.draft) !== this.appliedDraft;
	}
	savePending() {
		return this.commitTimer !== null || this.saving;
	}
	restarting(e) {
		return (e.status === "queued" || e.status === "running") && (this.savePending() || this.restartingRevision === e.revision);
	}
	showStatus() {
		let e = this.activeContext();
		if (!e) return;
		if (this.savePending()) {
			this.setMessage(ut(this.restarting(e.state)), "info");
			return;
		}
		let t = ot(e.state, q(e.page, e.state.stage), this.settingsNotApplied(), this.restarting(e.state));
		this.setMessage(t.text, t.tone);
	}
	draftEdited() {
		this.settingsNotApplied() && this.scheduleCommit(), this.saving || (this.settingsNotApplied() && this.previewMode === "result" ? this.showPreview("input") : this.refreshResultPane(), this.showStatus());
	}
	resultAvailable() {
		let e = this.activeContext();
		if (!e) return !1;
		let t = e.state;
		return q(e.page, t.stage).current && !this.settingsNotApplied() && !!(t.preview_storage_identifier || t.output_storage_identifier);
	}
	showPreview(e) {
		let t = this.activeContext();
		if (!t) return;
		let n = t.state, r = this.resultAvailable();
		this.previewMode = e, this.inputButton.setAttribute("aria-pressed", String(this.previewMode === "input")), this.resultButton.setAttribute("aria-pressed", String(this.previewMode === "result")), this.panes && (this.panes.dataset.pane = this.previewMode);
		let i = this.url("previewUrlTemplate", t.page.id, n.stage), a = i ? `${i}?input=true` : "";
		this.image.hidden = !a;
		let o = "";
		if (a) {
			let e = R.indexOf(n.stage), r = e > 0 ? R[e - 1] : void 0, i;
			i = r ? et(X(t.page, r)) : t.page.work_image_token ?? "";
			let s = i ? `v=${i}` : `revision=${n.revision}-${n.status}`;
			o = `${a}${a.includes("?") ? "&" : "?"}${s}`;
		}
		o !== this.previewUrl && (this.cancelPreviewReload(), this.previewRetry = G, this.previewUrl = o, o && (this.image.src = o));
		let s = r ? V(n.parameters) : "";
		this.warnings.textContent = s ? `Worker warning: ${s}` : "", this.warnings.hidden = !s, this.updateResultPane(t.page, n), this.applyDeskewPreview(), this.renderGeometry();
	}
	updateResultPane(e, t) {
		let n = this.resultImage;
		if (!n) return;
		let r = `${e.id}:${t.stage}`, i = e.id === this.activePageId && t.stage === this.activeStage, a = i && this.savePending(), o = i && !a && this.settingsNotApplied(), s = a ? ut(this.restarting(t)) : gt(t, q(e, t.stage), o), c = a || !o && (t.status === "queued" || t.status === "running"), l = this.url("previewUrlTemplate", e.id, t.stage), u = "";
		if (l && !s) {
			let e = et(t);
			u = `${l}?${e ? `v=${e}` : `revision=${t.revision}-${t.status}`}`;
		}
		u ? (this.resultKey = r, u !== this.resultUrl && (this.resultUrl = u, n.src = u), n.hidden = !1) : c && this.resultKey === r && this.resultUrl ? n.hidden = !1 : (o && this.resultKey === r && this.resultUrl || (this.resultUrl = "", this.resultKey = "", n.removeAttribute("src")), n.hidden = !0), this.resultPane?.classList.toggle("busy", c), this.setResultStatus(s, c);
	}
	refreshResultPane() {
		let e = this.activeContext();
		e && this.updateResultPane(e.page, e.state);
	}
	setResultStatus(e, t) {
		let n = this.resultStatus;
		if (!n) return;
		n.hidden = !e;
		let r = n.querySelector("[data-result-spinner]");
		r && (r.hidden = !t);
		let i = n.querySelector("[data-result-message]");
		i && (i.textContent = e);
	}
	updateActivePreview(e, t) {
		if (e.scan_page_id !== this.activePageId || e.stage !== this.activeStage) return;
		let n = e.status === "success" && (t?.status !== "success" || t.revision !== e.revision), r = this.pages.get(e.scan_page_id);
		if (this.dewarpOverlay?.isDragging() === !0 || this.contentOverlay?.isDragging() === !0 || this.deskewOverlay?.isDragging() === !0) r && this.updateResultPane(r, e);
		else if (this.showPreview(n ? "result" : this.previewMode), e.stage === "deskew") this.syncDeskewControls(), n && this.setMessage(xe(e.parameters), "success");
		else if (e.stage === "dewarping") {
			let t = this.controls.querySelector("[data-dewarp-result]");
			t && e.status === "success" && (t.textContent = ce(e.parameters)), n && this.setMessage(ce(e.parameters), "success");
		} else if (e.stage === "select_content") {
			if (this.updateContentText(), n) {
				let t = this.contentBox();
				this.setMessage(y(e.parameters, t, this.contentDpi()), "success");
			}
		} else if (e.stage === "margins") {
			let t = this.controls.querySelector("[data-margins-result]");
			t && (t.textContent = e.status === "success" ? Pe(e.parameters) : ""), n && this.setMessage(`Completed. ${Pe(e.parameters)}`, "success");
		} else n && this.setMessage("Completed. Result preview is ready.", "success");
	}
	markActiveControls() {
		this.root.querySelectorAll("[data-stage-select]").forEach((e) => {
			e.classList.toggle("active", e.dataset.stageSelect === this.activeStage);
		});
	}
	activeContext() {
		if (!this.activePageId) return null;
		let e = this.pages.get(this.activePageId), t = e ? X(e, this.activeStage) : void 0;
		return e && t ? {
			page: e,
			state: t
		} : null;
	}
	renderControls() {
		switch (this.controls.replaceChildren(), this.activeStage) {
			case "deskew":
				this.renderDeskewControls();
				break;
			case "select_content":
				this.renderContentControls();
				break;
			case "margins":
				this.renderMarginControls();
				break;
			case "dewarping":
				this.renderDewarpControls();
				break;
			case "output": this.renderOutputControls();
		}
	}
	renderDeskewControls() {
		let e = this.activeContext()?.state, t = "manual_angle" in this.draft, n = document.createElement("fieldset");
		n.className = "st-radio-group";
		let r = document.createElement("legend");
		r.textContent = "Mode", n.append(r);
		for (let e of ["auto", "manual"]) {
			let r = document.createElement("label");
			r.className = "checkbox";
			let i = document.createElement("input");
			i.type = "radio", i.name = "deskew-mode", i.value = e, i.checked = e === "manual" === t, i.disabled = !this.editable, i.dataset.deskewMode = e, i.addEventListener("change", () => {
				i.checked && (e === "manual" ? this.setDeskewAngle(this.deskewAngle(), !1) : (delete this.draft.manual_angle, this.applyDeskewPreview()), this.commitDraft());
			}), r.append(i, document.createTextNode(e === "auto" ? "Auto" : "Manual")), n.append(r);
		}
		this.controls.append(n);
		let i = document.createElement("label");
		i.textContent = "Angle (degrees, + = clockwise)";
		let a = document.createElement("input");
		a.type = "number", a.step = "0.01", a.min = "-45", a.max = "45", a.dataset.deskewAngle = "", a.disabled = !this.editable, a.value = this.deskewAngle().toFixed(2), a.title = "Mouse wheel: 0.1° per notch (Shift: 0.05°). Ctrl+wheel over the page also rotates.", a.addEventListener("input", () => {
			let e = Number(a.value);
			a.value !== "" && Number.isFinite(e) && (this.deskewOverlay?.flashGrid(), this.setDeskewAngle(me(e), !0, !1));
		}), a.addEventListener("wheel", (e) => {
			if (a.disabled) return;
			e.preventDefault();
			let t = _e(this.deskewAngle(), {
				deltaY: -(e.deltaY || e.deltaX),
				ctrlKey: !0,
				shiftKey: e.shiftKey
			});
			t !== null && (this.deskewOverlay?.flashGrid(), this.setDeskewAngle(t, !0));
		}, { passive: !1 }), i.append(a), this.controls.append(i);
		let o = document.createElement("label");
		o.className = "checkbox";
		let s = document.createElement("input");
		s.type = "checkbox", s.dataset.deskewGrid = "", s.checked = St(), s.addEventListener("change", () => {
			Ct(s.checked), this.deskewOverlay?.setKeepGrid(s.checked);
		}), o.append(s, document.createTextNode("Keep the grid visible")), this.controls.append(o);
		let c = document.createElement("p");
		c.className = "st-stage-result", c.dataset.deskewResult = "", c.textContent = e?.status === "success" ? xe(e.parameters) : "", this.controls.append(c);
	}
	deskewAngle() {
		if ("manual_angle" in this.draft) return P(J(this.draft.manual_angle, 0));
		let e = this.activeContext()?.state;
		return e ? P(J(e.parameters.angle, 0)) : 0;
	}
	deskewEditing() {
		return this.editable && this.activeStage === "deskew" && !this.image.hidden;
	}
	setDeskewAngle(e, t, n = !0) {
		let r = P(e), i = "manual_angle" in this.draft;
		if (this.draft.manual_angle = r, !i) {
			let e = this.controls.querySelector("[data-deskew-mode=\"manual\"]");
			e && (e.checked = !0);
		}
		if (n) {
			let e = this.controls.querySelector("[data-deskew-angle]");
			e && (e.value = r.toFixed(2));
		}
		let a = this.controls.querySelector("[data-deskew-result]");
		a && (a.textContent = `${be(r)} (manual, not saved yet).`), this.previewMode === "result" && this.showPreview("input"), this.applyDeskewPreview(), t && this.draftEdited();
	}
	scheduleCommit() {
		this.editable && (this.commitTimer !== null && window.clearTimeout(this.commitTimer), this.commitTimer = window.setTimeout(() => {
			this.commitTimer = null, this.commitDraft();
		}, 600));
	}
	async commitDraft() {
		if (!this.editable) return;
		if (this.commitTimer !== null && (window.clearTimeout(this.commitTimer), this.commitTimer = null), this.saving) {
			this.saveQueued = !0;
			return;
		}
		let e = this.leftDrafts.shift();
		e ? await this.saveDraft(e) : await this.saveActiveStage(), this.leftDrafts.length > 0 || this.saveQueued ? (this.saveQueued = !1, await this.commitDraft()) : this.revertAfterSave && (this.revertAfterSave = !1, this.activePageId && this.activate(this.activePageId, this.activeStage));
	}
	syncDeskewControls() {
		if (this.activeStage !== "deskew") return;
		let e = this.activeContext()?.state;
		if (!e) return;
		let t = this.controls.querySelector("[data-deskew-angle]");
		if (this.commitTimer !== null || this.saving || this.deskewOverlay?.isDragging() === !0) return;
		(t === null || document.activeElement !== t) && !("manual_angle" in this.draft) && t && (t.value = this.deskewAngle().toFixed(2));
		let n = this.controls.querySelector("[data-deskew-result]");
		n && e.status === "success" && !this.settingsNotApplied() && (n.textContent = xe(e.parameters)), this.applyDeskewPreview();
	}
	applyDeskewPreview() {
		if (this.activeStage !== "deskew" || this.image.hidden || !this.viewport || !this.image.naturalWidth) {
			this.image.style.transform = "", this.deskewOverlay && (this.deskewOverlay.element.style.display = "none");
			return;
		}
		let e = this.deskewAngle();
		this.image.style.transformOrigin = "50% 50%", this.image.style.transform = e ? `rotate(${e}deg)` : "", this.deskewOverlay || (this.deskewOverlay = new Ce(this.viewport, {
			onPreview: (e) => this.setDeskewAngle(e, !1),
			onCommit: (e) => {
				this.setDeskewAngle(e, !1), this.commitDraft();
			}
		}), this.deskewOverlay.setKeepGrid(St()));
		let t = this.deskewOverlay;
		t.element.style.display = "", t.element.classList.toggle("read-only", !this.editable);
		let n = this.image.naturalWidth * this.view.zoom, r = this.image.naturalHeight * this.view.zoom, i = {
			x: this.view.x + n / 2,
			y: this.view.y + r / 2
		}, a = Math.max(24, Math.min(n / 2, this.viewport.clientWidth / 2 - 16, this.viewport.clientHeight / 2 - 16));
		t.layout(i, a, e);
	}
	renderContentControls() {
		let e = this.activeContext()?.state, t = String(this.draft.content_mode ?? e?.parameters.content_mode ?? "auto"), n = document.createElement("fieldset");
		n.className = "st-radio-group";
		let r = document.createElement("legend");
		r.textContent = "Mode", n.append(r);
		for (let [e, r] of [
			["auto", "Auto"],
			["manual", "Manual"],
			["disabled", "Disabled"]
		]) {
			let i = document.createElement("label");
			i.className = "checkbox";
			let a = document.createElement("input");
			a.type = "radio", a.name = "content-mode", a.value = e, a.checked = t === e, a.disabled = !this.editable, a.dataset.contentMode = e, a.addEventListener("change", () => {
				if (a.checked) {
					if (this.draft.content_mode = e, e === "manual") {
						let e = this.contentBox() ?? this.defaultContentBox();
						this.draft.content_box = [
							e.x1,
							e.y1,
							e.x2,
							e.y2
						];
					} else delete this.draft.content_box;
					this.renderGeometry(), this.commitDraft();
				}
			}), i.append(a, document.createTextNode(r)), n.append(i);
		}
		this.controls.append(n);
		let i = document.createElement("p");
		i.className = "st-stage-result", i.dataset.contentResult = "", this.controls.append(i), this.updateContentText();
	}
	contentBox() {
		let e = this.activeContext()?.state, t = String(this.draft.content_mode ?? e?.parameters.content_mode ?? "auto");
		if (t === "disabled") return null;
		let n = {
			x1: 0,
			y1: 0,
			x2: 0,
			y2: 0
		};
		if (t === "manual" && this.draft.content_box !== void 0) {
			let e = _t(this.draft.content_box, n);
			return e.x2 > e.x1 && e.y2 > e.y1 ? e : null;
		}
		if (!e || e.status !== "success" || e.parameters.content_found === !1) return null;
		let r = _t(e.parameters.content_box, n);
		return r.x2 > r.x1 && r.y2 > r.y1 ? r : null;
	}
	defaultContentBox() {
		let e = this.sourceSize();
		return {
			x1: Math.round(e.width * .15),
			y1: Math.round(e.height * .15),
			x2: Math.round(e.width * .85),
			y2: Math.round(e.height * .85)
		};
	}
	contentDpi() {
		let e = this.activeContext()?.state.parameters.dpi;
		if (Array.isArray(e) && e.length >= 2) {
			let t = Y(e[0]), n = Y(e[1]);
			if (t && n) return {
				x: t,
				y: n
			};
		}
		return null;
	}
	updateContentText(e = this.contentBox()) {
		let t = this.controls.querySelector("[data-content-result]");
		if (!t) return;
		let n = this.activeContext()?.state, r = {
			...n?.parameters ?? {},
			...this.draft
		};
		t.textContent = n?.status === "success" || this.draft.content_mode === "manual" ? y(r, e, this.contentDpi()) : "";
	}
	renderContentBox(e, t) {
		let n = this.contentBox(), r = this.contentOverlay;
		r && (n || r.isDragging()) ? (n && r.setBox(n), r.element.parentElement !== this.geometryLayer && this.geometryLayer.append(r.element), r.layout(e, t)) : (this.removeContentOverlay(), n && (this.contentOverlay = new x(this.geometryLayer, n, this.editable, {
			onPreview: (e) => this.updateContentText(e),
			onCommit: (e) => {
				this.draft.content_mode = "manual", this.draft.content_box = [
					e.x1,
					e.y1,
					e.x2,
					e.y2
				];
				let t = this.controls.querySelector("[data-content-mode=\"manual\"]");
				t && (t.checked = !0), this.updateContentText(e), this.commitDraft();
			}
		}), this.contentOverlay.layout(e, t)));
	}
	renderMarginControls() {
		let e = je(this.draft);
		Object.assign(this.draft, e);
		let t = {
			horizontal: this.marginLinks.horizontal ?? e.left_mm === e.right_mm,
			vertical: this.marginLinks.vertical ?? e.top_mm === e.bottom_mm
		}, n = /* @__PURE__ */ new Map(), r = document.createElement("fieldset");
		r.className = "st-margins";
		let i = document.createElement("legend");
		i.textContent = "Margins (mm)", r.append(i);
		for (let i of we) {
			let a = document.createElement("label");
			a.textContent = i[0].toUpperCase() + i.slice(1);
			let o = document.createElement("input");
			o.type = "number", o.min = "0", o.max = "500", o.step = "0.1", o.value = String(e[`${i}_mm`]), o.disabled = !this.editable, o.dataset.margin = i, o.addEventListener("input", () => {
				if (!o.validity.valid || o.value === "") return;
				let e = Number(o.value);
				if (this.draft[`${i}_mm`] = e, t[i === "left" || i === "right" ? "horizontal" : "vertical"]) {
					let t = Fe(i);
					this.draft[`${t}_mm`] = e;
					let r = n.get(t);
					r && (r.value = o.value);
				}
			}), n.set(i, o), a.append(o), r.append(a);
		}
		for (let [e, n] of [["vertical", "Link top and bottom"], ["horizontal", "Link left and right"]]) {
			let i = document.createElement("label");
			i.className = "checkbox";
			let a = document.createElement("input");
			a.type = "checkbox", a.checked = t[e], a.disabled = !this.editable, a.dataset.marginLink = e, a.addEventListener("change", () => {
				t[e] = a.checked, this.marginLinks[e] = a.checked;
			}), i.append(a, document.createTextNode(n)), r.append(i);
		}
		this.controls.append(r);
		let a = document.createElement("label");
		a.className = "checkbox";
		let o = document.createElement("input");
		o.type = "checkbox", o.checked = e.align_with_others, o.disabled = !this.editable, o.dataset.alignWithOthers = "", a.append(o, document.createTextNode("Align with other pages")), this.controls.append(a);
		let s = document.createElement("fieldset");
		s.className = "st-align-grid";
		let c = document.createElement("legend");
		c.textContent = "Alignment", s.append(c);
		let l = [];
		for (let t of [
			"top",
			"center",
			"bottom"
		]) for (let n of [
			"left",
			"center",
			"right"
		]) {
			let r = document.createElement("input");
			r.type = "radio", r.name = "margins-alignment", r.value = `${t} ${n}`, r.title = t === n ? "centre" : `${t} ${n}`, r.setAttribute("aria-label", r.title), r.checked = e.align_y === t && e.align_x === n, r.dataset.align = `${t}-${n}`, r.addEventListener("change", () => {
				r.checked && (this.draft.align_x = n, this.draft.align_y = t);
			}), l.push(r), s.append(r);
		}
		let u = () => {
			for (let e of l) e.disabled = !this.editable || !o.checked;
		};
		o.addEventListener("change", () => {
			this.draft.align_with_others = o.checked, u();
		}), u(), this.controls.append(s);
		let d = document.createElement("p");
		d.className = "st-stage-result", d.dataset.marginsResult = "";
		let f = this.activeContext()?.state;
		d.textContent = f?.status === "success" ? Pe(f.parameters ?? {}) : "", this.controls.append(d);
	}
	renderDewarpControls() {
		let e = this.activeContext()?.state, t = String(this.draft.dewarp_mode ?? e?.parameters.dewarp_mode ?? "auto"), n = document.createElement("fieldset");
		n.className = "st-radio-group";
		let r = document.createElement("legend");
		r.textContent = "Mode", n.append(r);
		for (let [e, r] of [
			["off", "Off"],
			["auto", "Auto"],
			["manual", "Manual"]
		]) {
			let i = document.createElement("label");
			i.className = "checkbox";
			let a = document.createElement("input");
			a.type = "radio", a.name = "dewarp-mode", a.value = e, a.checked = t === e || e === "off" && t === "bypass", a.disabled = !this.editable, a.dataset.dewarpMode = e, a.addEventListener("change", () => {
				if (a.checked) {
					if (this.draft.dewarp_mode = e, e === "manual") {
						let e = this.dewarpCurves();
						this.draft.top_curve = e.top.points.map(([e, t]) => [e, t]), this.draft.bottom_curve = e.bottom.points.map(([e, t]) => [e, t]);
					} else delete this.draft.top_curve, delete this.draft.bottom_curve;
					this.renderGeometry(), this.commitDraft();
				}
			}), i.append(a, document.createTextNode(r)), n.append(i);
		}
		this.controls.append(n);
		let i = document.createElement("label");
		i.textContent = "Depth perception";
		let a = document.createElement("input");
		a.type = "range", a.min = "1", a.max = "3", a.step = "0.1", a.dataset.depthPerception = "", a.disabled = !this.editable, a.value = String(J(this.draft.depth_perception ?? e?.parameters.depth_perception, 2)), a.addEventListener("input", () => {
			this.draft.depth_perception = Number(a.value), this.dewarpOverlay && (this.dewarpOverlay.depth = Number(a.value), this.dewarpOverlay.draw());
		}), a.addEventListener("change", () => void this.commitDraft()), i.append(a), this.controls.append(i);
		let o = document.createElement("p");
		o.className = "st-stage-result", o.dataset.dewarpResult = "", o.textContent = e?.status === "success" ? ce(e.parameters) : "", this.controls.append(o);
	}
	dewarpCurves() {
		let e = this.activeContext()?.state?.parameters ?? {}, t = (e) => {
			if (!Array.isArray(e) || e.length < 2) return null;
			let t = [];
			for (let n of e) {
				if (!Array.isArray(n) || n.length < 2) return null;
				let e = Number(n[0]), r = Number(n[1]);
				if (!Number.isFinite(e) || !Number.isFinite(r)) return null;
				t.push([e, r]);
			}
			return t;
		}, n = String(this.draft.dewarp_mode ?? "") === "manual", r = t(n ? this.draft.top_curve : e.top_curve) ?? t(e.top_curve), i = t(n ? this.draft.bottom_curve : e.bottom_curve) ?? t(e.bottom_curve);
		if (r && i) return {
			top: new C(r),
			bottom: new C(i)
		};
		let a = this.sourceSize(), o = (e) => [
			0,
			.25,
			.5,
			.75,
			1
		].map((t) => [t * a.width, e]);
		return {
			top: new C(o(0)),
			bottom: new C(o(a.height))
		};
	}
	renderDewarpOverlay(e, t) {
		let n = this.activeContext()?.state, r = String(this.draft.dewarp_mode ?? n?.parameters.dewarp_mode ?? "auto");
		if (r === "off" || r === "bypass") {
			this.removeDewarpOverlay();
			return;
		}
		let i = J(this.draft.depth_perception ?? n?.parameters.depth_perception, 2), a = this.dewarpCurves(), o = this.dewarpOverlay;
		if (o) {
			o.setCurves(a.top, a.bottom, i), o.element.parentElement !== this.geometryLayer && this.geometryLayer.append(o.element), o.layout(e, t);
			return;
		}
		let s = new N(this.geometryLayer, a.top, a.bottom, this.editable, {
			onPreview: () => void 0,
			onCommit: (e, t) => {
				this.draft.dewarp_mode = "manual", this.draft.top_curve = e.points.map(([e, t]) => [e, t]), this.draft.bottom_curve = t.points.map(([e, t]) => [e, t]);
				let n = this.controls.querySelector("[data-dewarp-mode=\"manual\"]");
				n && (n.checked = !0), this.commitDraft();
			}
		});
		s.depth = i, this.dewarpOverlay = s, s.layout(e, t);
	}
	removeDewarpOverlay() {
		this.dewarpOverlay?.remove(), this.dewarpOverlay = null;
	}
	renderOutputControls() {
		let e = [
			"otsu",
			"sauvola",
			"wolf",
			"fox",
			"window",
			"grad",
			"copy"
		];
		this.addSelect("Output algorithm", "output_mode", e, "otsu"), this.addCheckbox("Keep color", "keep_color", !1), this.addCheckbox("Invert output", "invert", !1), this.addNumber("Local window size", "window_size", 3, 501, 2, 35), this.addNumber("Threshold k", "k", -2, 2, .01, .33), this.addNumber("Threshold r", "r", 1, 512, 1, 128), this.addSelect("Text / picture zones", "zone_mode", [
			"auto",
			"manual",
			"off"
		], "auto", !0);
		let t = qe(this.activeContext()?.state.parameters ?? {});
		if (this.draft.zone_mode !== "off" && (this.addSelect("Text algorithm", "text_algorithm", e, "sauvola"), this.addSelect("Picture algorithm (Keep color uses copy)", "picture_algorithm", e, "copy")), this.draft.zone_mode === "manual") {
			"zones" in this.draft || (this.draft.zones = t);
			let e = qe(this.draft);
			this.draft.zones = e, e.forEach((t, n) => {
				this.addSelectValue(`Region ${n + 1} type`, t.kind, ["text", "picture"], (e) => {
					t.kind = e, this.renderGeometry();
				});
				for (let [e, r] of [
					"Left",
					"Top",
					"Right",
					"Bottom"
				].entries()) {
					let i = document.createElement("label");
					i.textContent = `Region ${n + 1} ${r}`;
					let a = document.createElement("input");
					a.type = "number", a.min = "0", a.value = String(t.box[e]), a.addEventListener("change", () => {
						t.box[e] = Math.max(0, J(a.value, t.box[e] ?? 0)), this.renderGeometry();
					}), i.append(a), this.controls.append(i);
				}
				this.addZoneButton(`Remove region ${n + 1}`, () => {
					e.splice(n, 1), this.renderControls(), this.renderGeometry();
				});
			});
			for (let t of ["text", "picture"]) this.addZoneButton(`Add ${t} region`, () => {
				if (e.length >= 64) return;
				let n = this.sourceSize();
				e.push({
					kind: t,
					box: [
						Math.round(n.width * .1),
						Math.round(n.height * .1),
						Math.round(n.width * .5),
						Math.round(n.height * .5)
					]
				}), this.renderControls(), this.renderGeometry();
			});
		} else this.draft.zone_mode === "auto" && this.addZoneButton(`Edit detected regions (${t.length})`, () => {
			this.draft.zone_mode = "manual", this.draft.zones = qe(this.activeContext()?.state.parameters ?? {}), this.showPreview("input"), this.renderControls(), this.renderGeometry();
		});
	}
	addZoneButton(e, t) {
		let n = document.createElement("button");
		n.type = "button", n.textContent = e, n.addEventListener("click", () => {
			t(), this.draftEdited();
		}), this.controls.append(n);
	}
	addSelect(e, t, n, r, i = !1) {
		let a = typeof this.draft[t] == "string" ? String(this.draft[t]) : r;
		t in this.draft || (this.draft[t] = a), this.addSelectValue(e, a, n, (e) => {
			this.draft[t] = e, i && (this.renderControls(), this.renderGeometry());
		});
	}
	addSelectValue(e, t, n, r) {
		let i = document.createElement("label");
		i.textContent = e;
		let a = document.createElement("select");
		for (let e of n) {
			let n = document.createElement("option");
			n.value = e, n.textContent = e.replaceAll("_", " "), n.selected = e === t, a.append(n);
		}
		a.addEventListener("change", () => r(a.value)), i.append(a), this.controls.append(i);
	}
	addNumber(e, t, n, r, i = 1, a = 0) {
		let o = document.createElement("label");
		o.textContent = e;
		let s = document.createElement("input");
		s.type = "number", s.value = String(J(this.draft[t], a)), s.step = String(i), n !== void 0 && (s.min = String(n)), r !== void 0 && (s.max = String(r)), this.draft[t] = Number(s.value), s.addEventListener("input", () => {
			s.validity.valid && s.value !== "" && (this.draft[t] = Number(s.value), this.renderGeometry());
		}), o.append(s), this.controls.append(o);
	}
	addCheckbox(e, t, n) {
		let r = document.createElement("label");
		r.className = "checkbox";
		let i = document.createElement("input");
		i.type = "checkbox", i.checked = typeof this.draft[t] == "boolean" ? !!this.draft[t] : n, this.draft[t] = i.checked, i.addEventListener("change", () => {
			this.draft[t] = i.checked;
		}), r.append(i, document.createTextNode(e)), this.controls.append(r);
	}
	sourceSize() {
		let e = this.activeContext(), t = {
			width: Math.max(1, this.image.naturalWidth),
			height: Math.max(1, this.image.naturalHeight)
		};
		if (!e) return t;
		let n = e.state.parameters, r = Y(n.input_width ?? n.source_width), i = Y(n.input_height ?? n.source_height);
		if (r && i) return {
			width: r,
			height: i
		};
		let a = R.indexOf(this.activeStage);
		if (a > 0) {
			let t = R[a - 1];
			if (t) {
				let n = X(e.page, t)?.parameters ?? {}, r = Y(n.output_width ?? n.resolved_width ?? n.canvas_width), i = Y(n.output_height ?? n.content_area_height ?? n.canvas_height);
				if (r && i) return {
					width: r,
					height: i
				};
			}
		}
		return t;
	}
	displaySize() {
		return {
			width: Math.max(1, this.image.clientWidth),
			height: Math.max(1, this.image.clientHeight)
		};
	}
	renderGeometry() {
		if (this.clearGeometry(), !this.image.complete || !this.image.naturalWidth || this.image.hidden) return;
		let e = this.sourceSize(), t = this.displaySize();
		switch (this.geometryLayer.style.width = `${t.width}px`, this.geometryLayer.style.height = `${t.height}px`, this.activeStage) {
			case "select_content":
				this.renderContentBox(e, t);
				break;
			case "dewarping":
				this.renderDewarpOverlay(e, t);
				break;
			case "output": {
				let n = this.draft.zone_mode === "manual";
				qe(n ? this.draft : this.activeContext()?.state.parameters ?? {}).forEach((r, i) => {
					let a = _t(r.box, {
						x1: 0,
						y1: 0,
						x2: e.width,
						y2: e.height
					});
					if (n) this.renderRectangle("zones", `${r.kind} ${i + 1}`, r.kind, e, t, 0, 1, a, (e) => {
						let t = this.draft.zones;
						t[i].box = [
							e.x1,
							e.y1,
							e.x2,
							e.y2
						];
					});
					else if (this.draft.zone_mode === "auto") {
						let n = document.createElement("div");
						n.className = `geometry-rectangle geometry-${r.kind}`, n.style.pointerEvents = "none", n.textContent = `Detected ${r.kind} ${i + 1}`, this.positionRectangle(n, We(a, e, t)), this.geometryLayer.append(n);
					}
				});
				break;
			}
		}
	}
	clearGeometry() {
		for (let e of this.interactions) e.unset();
		this.interactions = [];
		let e = this.activeStage === "select_content" ? this.contentOverlay : null, t = this.activeStage === "dewarping" ? this.dewarpOverlay : null;
		e || this.removeContentOverlay(), t || this.removeDewarpOverlay();
		for (let n of [...this.geometryLayer.children]) n !== e?.element && n !== t?.element && n.remove();
	}
	removeContentOverlay() {
		this.contentOverlay?.remove(), this.contentOverlay = null;
	}
	renderRectangle(e, t, n, r, i, a, o = .92, s, l) {
		let u = {
			x1: r.width * a,
			y1: r.height * .1,
			x2: r.width * o,
			y2: r.height * .9
		}, d = s ?? _t(this.draft[e], u);
		l || (this.draft[e] = [
			d.x1,
			d.y1,
			d.x2,
			d.y2
		]);
		let f = We(d, r, i), p = document.createElement("div");
		p.className = `geometry-rectangle geometry-${n}`, p.setAttribute("aria-label", `${t} region; drag or resize`);
		let m = document.createElement("span");
		m.textContent = t, p.append(m), this.positionRectangle(p, f), this.geometryLayer.append(p);
		let h = () => {
			let t = Ge(this.elementBox(p), r, i);
			l ? l(t) : this.draft[e] = [
				t.x1,
				t.y1,
				t.x2,
				t.y2
			], this.draftEdited();
		}, g = c.default.modifiers.restrictRect({ restriction: "parent" }), _ = (0, c.default)(p).draggable({
			modifiers: [g],
			listeners: { move: (e) => {
				let t = this.elementBox(p), n = t.x2 - t.x1, r = t.y2 - t.y1, a = B(t.x1 + e.dx, 0, i.width - n), o = B(t.y1 + e.dy, 0, i.height - r);
				this.positionRectangle(p, {
					x1: a,
					y1: o,
					x2: a + n,
					y2: o + r
				}), h();
			} }
		}).resizable({
			edges: {
				left: !0,
				right: !0,
				bottom: !0,
				top: !0
			},
			modifiers: [c.default.modifiers.restrictEdges({ outer: "parent" }), c.default.modifiers.restrictSize({ min: {
				width: 24,
				height: 24
			} })],
			listeners: { move: (e) => {
				let t = this.geometryLayer.getBoundingClientRect();
				this.positionRectangle(p, {
					x1: e.rect.left - t.left,
					y1: e.rect.top - t.top,
					x2: e.rect.right - t.left,
					y2: e.rect.bottom - t.top
				}), h();
			} }
		});
		this.interactions.push(_);
	}
	positionRectangle(e, t) {
		e.style.left = `${t.x1}px`, e.style.top = `${t.y1}px`, e.style.width = `${Math.max(1, t.x2 - t.x1)}px`, e.style.height = `${Math.max(1, t.y2 - t.y1)}px`;
	}
	elementBox(e) {
		let t = J(e.style.left.replace("px", ""), 0), n = J(e.style.top.replace("px", ""), 0);
		return {
			x1: t,
			y1: n,
			x2: t + J(e.style.width.replace("px", ""), e.offsetWidth),
			y2: n + J(e.style.height.replace("px", ""), e.offsetHeight)
		};
	}
	async saveActiveStage() {
		let e = this.activeSnapshot();
		e && this.activeContext() && this.editable && (e.sent === this.appliedDraft ? (this.syncDeskewControls(), this.showStatus(), this.refreshResultPane()) : await this.saveDraft(e));
	}
	async saveDraft(e) {
		let t = this.pages.get(e.pageId), n = t ? X(t, e.stage) : void 0, r = this.url("stageUrlTemplate", e.pageId, e.stage);
		if (!t || !n || !r || !this.editable) return;
		let { pageId: i, stage: a, sent: o } = e, s = () => this.activePageId === i && this.activeStage === a, c = n.status === "queued" || n.status === "running";
		this.saving = !0, s() && this.setMessage(ut(c), "info");
		try {
			let i = await fetch(r, {
				method: "POST",
				credentials: "same-origin",
				headers: {
					"Content-Type": "application/json",
					"X-CSRFToken": Z()
				},
				body: JSON.stringify({
					expected_revision: n.revision,
					algorithm: n.algorithm,
					parameters: e.draft
				})
			});
			if (i.status === 409 && !s()) {
				await this.reloadPageData(r), this.setMessage(`Conflict detected: another editor changed page ${t.logical_sequence_number} · ${z[a]}. Your last change there was not saved.`, "conflict");
				return;
			}
			if (i.status === 409) {
				this.commitTimer !== null && window.clearTimeout(this.commitTimer), this.commitTimer = null, this.saveQueued = !1, await this.refreshPage(r), await this.ensuring, this.setMessage("Conflict detected: another editor changed this stage. Latest values loaded; review before changing them again.", "conflict");
				return;
			}
			if (!i.ok) throw Error(await i.text());
			let l = await i.json(), u = s();
			if (u && (this.appliedDraft = o, this.restartingRevision = c && l.stage.status !== "success" ? l.stage.revision : null), this.mergeStage(l.stage), !u) return;
			let d = l.waiting_for;
			if (d && bt(d)) {
				this.setMessage(`Saved. Waiting for ${z[d]}; this stage runs when it is done.`, "success");
				return;
			}
			let f = this.activeContext()?.state ?? l.stage;
			if (f.status !== "success") {
				let e = this.pages.get(f.scan_page_id), t = ot(f, e ? q(e, f.stage) : void 0, !1, this.restartingRevision === f.revision);
				this.setMessage(t.text, t.tone);
				return;
			}
			this.setMessage(f.stage === "deskew" ? `Saved. ${xe(f.parameters)}` : f.stage === "dewarping" ? `Saved. ${ce(f.parameters)}` : f.stage === "select_content" ? `Saved. ${y(f.parameters, this.contentBox(), this.contentDpi())}` : "Saved. Result preview is ready.", "success");
		} catch (e) {
			this.setMessage(e instanceof Error ? e.message : "Stage update failed.", "error");
		} finally {
			this.saving = !1, this.saveQueued || (this.syncDeskewControls(), this.refreshResultPane(), (this.activePageId !== i || this.activeStage !== a) && this.showStatus());
		}
	}
	async applyBatch() {
		if (!this.editable) return;
		let e = this.url("batchUrlTemplate", "", this.activeStage);
		if (!e) return;
		let t = ve(this.batchScope.value || "selected", this.strip?.displayOrder() ?? this.naturalOrder, this.activePageId, this.selectedPages);
		if (t !== null && t.length === 0) {
			this.setMessage("Select at least one page before applying a batch.", "error");
			return;
		}
		this.batchButton.disabled = !0, this.setMessage("Applying stage settings to the batch…", "info");
		let n = {
			parameters: this.draft,
			preserve_exceptions: this.preserveExceptions.checked
		};
		t !== null && (n.page_ids = t);
		try {
			let t = await fetch(e, {
				method: "POST",
				credentials: "same-origin",
				headers: {
					"Content-Type": "application/json",
					"X-CSRFToken": Z()
				},
				body: JSON.stringify(n)
			});
			if (!t.ok) throw Error(await t.text());
			let r = await t.json();
			for (let e of r.applied) this.mergeStage(e.stage);
			let i = r.applied.filter((e) => e.waiting_for).length;
			this.setMessage(`Saved for ${r.applied_count} page(s): ${r.jobs.length} queued, ${i} waiting for an earlier stage; preserved ${r.skipped_count} manual exception(s).`, "success");
		} catch (e) {
			this.setMessage(e instanceof Error ? e.message : "Batch update failed.", "error");
		} finally {
			this.batchButton.disabled = !this.editable;
		}
	}
	async refreshPage(e) {
		let t = await this.reloadPageData(e);
		this.activate(t.id, this.activeStage);
	}
	async reloadPageData(e) {
		let t = await fetch(e, { credentials: "same-origin" });
		if (!t.ok) throw Error("Could not load the latest stage revision.");
		let n = await t.json();
		return this.pages.set(n.id, n), n;
	}
	mergeStage(e) {
		let t = this.pages.get(e.scan_page_id);
		if (!t) return;
		let n = t.stages.findIndex((t) => t.stage === e.stage), r = n >= 0 ? t.stages[n] : void 0;
		if (r && ht(e, r) || (n >= 0 ? t.stages[n] = e : t.stages.push(e), this.updateStageDom(e), this.updateActivePreview(e, r), e.scan_page_id !== this.activePageId || this.saving)) return;
		let i = e.status === "success" && (r?.status !== "success" || r.revision !== e.revision);
		e.stage === this.activeStage ? i || this.showStatus() : (e.stage === H(this.activeStage) && !this.isDragging() ? this.showPreview(this.previewMode) : this.refreshResultPane(), this.showStatus());
	}
	isDragging() {
		return this.deskewOverlay?.isDragging() === !0 || this.contentOverlay?.isDragging() === !0 || this.dewarpOverlay?.isDragging() === !0;
	}
	updateStageDom(e) {
		let t = this.pages.get(e.scan_page_id);
		t && this.strip && this.strip.update(this.stripItem(t));
	}
	stripItem(e) {
		let t = nt(e, this.activeStage), n = e.active_version?.id, r = "";
		if (t) {
			let n = this.url("stageThumbnailTemplate", e.id, t.stage);
			n && (r = `${n}?v=${et(t)}`);
		} else if (n && e.work_image_token) {
			let t = this.url("pageThumbnailTemplate", e.id);
			t && (r = `${t}?v=${e.work_image_token}`);
		} else r = e.thumbnail ?? "";
		let i = e.side && e.side !== "single" ? ` ${e.side}` : "", a = e.recognized_page_number ? `p. ${e.recognized_page_number}` : "", o = rt(e, this.activeStage);
		return {
			id: e.id,
			label: `${e.logical_sequence_number}${i}`,
			caption: e.active_version?.original_filename || a,
			thumbnailUrl: r,
			width: o.width,
			height: o.height,
			stages: R.map((t) => {
				let n = X(e, t), r = q(e, t);
				return {
					stage: t,
					label: z[t],
					status: n?.status ?? "unknown",
					manual: n?.manual ?? !1,
					last_error: n?.last_error ?? "",
					waiting_for: r.waitingFor ? z[r.waitingFor] : "",
					stale_reason: r.staleReason
				};
			})
		};
	}
	refreshStrip() {
		if (!this.strip) return;
		let e = this.naturalOrder.map((e) => this.pages.get(e)).filter((e) => e !== void 0).map((e) => this.stripItem(e)), t = this.sortSelect?.value ?? "natural";
		this.strip.setItems(e, f(e, t));
	}
	bindStrip() {
		this.sortSelect?.addEventListener("change", () => {
			this.refreshStrip(), this.strip?.setActive(this.activePageId, new Set(this.selectedPages), !0);
		}), this.refreshStrip(), document.addEventListener("keydown", (e) => {
			if (e.target?.closest("input, select, textarea, [contenteditable='true']") || e.ctrlKey || e.metaKey || e.altKey) return;
			let t = {
				ArrowDown: 1,
				PageDown: 1,
				w: 1,
				W: 1,
				ArrowUp: -1,
				PageUp: -1,
				q: -1,
				Q: -1,
				Home: -Infinity,
				End: Infinity
			}[e.key];
			if (t === void 0 || !this.strip) return;
			let n = this.strip.displayOrder(), r = Number.isFinite(t) ? m(n, this.activePageId, t) : t < 0 ? n[0] ?? null : n[n.length - 1] ?? null;
			r && (e.preventDefault(), this.selectedPages.clear(), this.activate(r, this.activeStage));
		});
	}
	clickPage(e, t) {
		let n = this.strip?.displayOrder() ?? this.naturalOrder, r = p({
			selected: this.selectedPages,
			anchor: this.selectionAnchor ?? this.activePageId
		}, n, e, t);
		this.selectedPages.clear();
		for (let e of r.selected) this.selectedPages.add(e);
		this.selectionAnchor = r.anchor, e === this.activePageId ? this.strip?.setActive(e, new Set(this.selectedPages), !1) : this.activate(e, this.activeStage), this.updateSelectionCount();
	}
	bindCanvas() {
		let e = this.viewport;
		if (!e) return;
		this.image.draggable = !1, this.resultImage && (this.resultImage.draggable = !1);
		for (let t of [e, this.resultViewport]) t && this.bindPanZoom(t, t === e);
		let t = (e, t) => {
			this.root.querySelector(e)?.addEventListener("click", t);
		}, n = () => {
			let e = this.paneExtent();
			return {
				x: e.width / 2,
				y: e.height / 2
			};
		};
		t("[data-zoom-in]", () => {
			this.view = Be(this.view, 1.25, n()), this.applyView();
		}), t("[data-zoom-out]", () => {
			this.view = Be(this.view, .8, n()), this.applyView();
		}), t("[data-zoom-fit]", () => this.fitToPane()), t("[data-zoom-actual]", () => {
			this.view = Be(this.view, 1 / this.view.zoom, n()), this.applyView();
		});
	}
	bindPanZoom(e, t) {
		e.addEventListener("wheel", (n) => {
			if (n.ctrlKey) {
				if (t && this.deskewEditing()) {
					n.preventDefault();
					let e = _e(this.deskewAngle(), n);
					e !== null && (this.deskewOverlay?.flashGrid(), this.setDeskewAngle(e, !0));
				}
				return;
			}
			n.preventDefault();
			let r = e.getBoundingClientRect();
			this.view = Be(this.view, Ve(n.deltaY, n.deltaMode), {
				x: n.clientX - r.left,
				y: n.clientY - r.top
			}), this.applyView();
		}, { passive: !1 });
		let n = null;
		e.addEventListener("pointerdown", (t) => {
			let r = t.target;
			t.button !== 0 || r?.closest("[data-geometry-layer] > *, [data-captures-pointer]") || (n = {
				id: t.pointerId,
				x: t.clientX,
				y: t.clientY
			}, e.setPointerCapture(t.pointerId), e.classList.add("panning"));
		}), e.addEventListener("pointermove", (e) => {
			n && n.id === e.pointerId && (this.view = He(this.view, e.clientX - n.x, e.clientY - n.y), n = {
				id: e.pointerId,
				x: e.clientX,
				y: e.clientY
			}, this.applyView(!1));
		});
		let r = (t) => {
			n && n.id === t.pointerId && (n = null, e.classList.remove("panning"));
		};
		e.addEventListener("pointerup", r), e.addEventListener("pointercancel", r);
	}
	paneExtent() {
		for (let e of [this.viewport, this.resultViewport]) if (e && e.clientWidth > 0) return {
			width: e.clientWidth,
			height: e.clientHeight
		};
		return {
			width: 0,
			height: 0
		};
	}
	fitToPane() {
		let e = this.image.naturalWidth ? this.image : this.resultImage;
		!this.viewport || !e?.naturalWidth ? this.renderGeometry() : (this.view = ze(this.paneExtent(), {
			width: e.naturalWidth,
			height: e.naturalHeight
		}), this.applyView());
	}
	applyView(e = !0) {
		this.applyResultView(), !this.surface || !this.image.naturalWidth || (this.image.style.width = `${Math.round(this.image.naturalWidth * this.view.zoom)}px`, this.image.style.height = `${Math.round(this.image.naturalHeight * this.view.zoom)}px`, this.surface.style.transform = `translate(${Math.round(this.view.x)}px, ${Math.round(this.view.y)}px)`, this.zoomLevel && (this.zoomLevel.textContent = `${Math.round(this.view.zoom * 100)} %`), this.applyDeskewPreview()), e && this.renderGeometry();
	}
	applyResultView() {
		let e = this.resultImage;
		if (!e || !this.resultSurface || !e.naturalWidth) return;
		let t = this.view.zoom, n = e.naturalWidth * t, r = e.naturalHeight * t, i = this.image.naturalWidth ? this.image.naturalWidth * t : n, a = this.image.naturalHeight ? this.image.naturalHeight * t : r, o = this.view.x + (i - n) / 2, s = this.view.y + (a - r) / 2;
		e.style.width = `${Math.round(n)}px`, e.style.height = `${Math.round(r)}px`, this.resultSurface.style.transform = `translate(${Math.round(o)}px, ${Math.round(s)}px)`, this.zoomLevel && (this.zoomLevel.textContent = `${Math.round(t * 100)} %`);
	}
	async runStageForAllPages(e, t) {
		if (!this.editable) return;
		let n = this.url("batchUrlTemplate", "", e);
		if (n) {
			t.disabled = !0, this.setMessage(`Running ${z[e]} for all pages…`, "info");
			try {
				let t = await fetch(n, {
					method: "POST",
					credentials: "same-origin",
					headers: {
						"Content-Type": "application/json",
						"X-CSRFToken": Z()
					},
					body: JSON.stringify({ preserve_exceptions: !1 })
				});
				if (!t.ok) throw Error(await t.text());
				let r = await t.json();
				for (let e of r.applied) this.mergeStage(e.stage);
				let i = r.applied.filter((e) => e.waiting_for).length;
				this.setMessage(`Queued ${z[e]} for ${r.jobs.length} of ${r.applied_count} page(s); ${i} waiting for an earlier stage.`, "success");
			} catch (e) {
				this.setMessage(e instanceof Error ? e.message : "Batch run failed.", "error");
			} finally {
				t.disabled = !this.editable;
			}
		}
	}
	startLiveUpdates() {
		let e = this.root.dataset.eventsUrl;
		if (e && typeof EventSource < "u") {
			this.eventSource = new EventSource(e), this.eventSource.addEventListener("open", () => {
				this.liveStatus.textContent = "Live updates connected", this.liveStatus.className = "live-status connected";
			}), this.eventSource.addEventListener("workspace", (e) => {
				let t = JSON.parse(e.data);
				for (let e of t.stages) this.mergeStage(e);
			}), this.eventSource.addEventListener("error", () => {
				this.liveStatus.textContent = "Live updates reconnecting…", this.liveStatus.className = "live-status reconnecting";
			});
			return;
		}
		let t = this.root.dataset.workspaceStateUrl;
		t && (this.liveStatus.textContent = "Live updates unavailable; using compatibility polling", this.pollingTimer = window.setInterval(() => void this.pollWorkspace(t), 5e3));
	}
	async pollWorkspace(e) {
		try {
			let t = this.pollCursor ? `${e}?since=${encodeURIComponent(this.pollCursor)}` : e, n = await fetch(t, { credentials: "same-origin" });
			if (!n.ok) return;
			let r = await n.json();
			for (let e of r.stages) this.mergeStage(e);
			this.pollCursor = r.cursor;
		} catch {
			this.liveStatus.textContent = "Workspace refresh failed; retrying…";
		}
	}
	updateSelectionCount() {
		this.selectionCount.textContent = `${this.selectedPages.size} page(s) selected`, this.batchButton.disabled = !this.editable || this.batchScope.value === "selected" && this.selectedPages.size === 0;
	}
	setMessage(e, t) {
		this.message.textContent = e, this.message.className = `editor-message editor-message-${t}`;
	}
	disableEditor(e) {
		this.image.hidden = !0, this.resultImage && (this.resultImage.hidden = !0), this.controls.replaceChildren(), this.revertButton && (this.revertButton.disabled = !0), this.batchButton.disabled = !0, this.setMessage(e, "info");
	}
};
function Tt() {
	let e = document.querySelector("#scantailor-workspace-data");
	if (!e?.textContent) return [];
	let t = JSON.parse(e.textContent);
	return Array.isArray(t) ? t : [];
}
function Et(e) {
	new wt(e, Tt()).start();
}
var Dt = document.querySelector("[data-scantailor-editor]");
Dt && Et(Dt);
export { dt as AUTOSAVE_DELAY_MS, G as INITIAL_PREVIEW_RETRY, W as PREVIEW_RETRY_EXHAUSTED_MESSAGE, Je as PREVIEW_RETRY_MAX_ATTEMPTS, lt as RESTARTING_MESSAGE, ct as RUNNING_MESSAGE, st as SETTINGS_NOT_APPLIED, R as STAGE_ORDER, et as artifactToken, B as clamp, Ge as displayBoxToSource, Ke as editableParameters, tt as fillUrl, Et as initializeScanTailorWorkspace, ht as isOlderStageState, Qe as nextPreviewRetry, _t as normalizeBox, qe as outputZonesForEditing, U as previewErrorMessage, Ze as previewIsBeingPrepared, $e as previewRetryUrl, H as previousStageName, gt as resultPaneMessage, ut as savingMessage, We as sourceBoxToDisplay, ft as stableJson, K as stageErrorText, q as stageReadiness, rt as stageSize, it as stageStatusLabel, ot as stageStatusMessage, V as stageWorkerWarnings, nt as stripSourceStage };

//# sourceMappingURL=scantailor-workspace.js.map