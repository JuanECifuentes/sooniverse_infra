/* Smoke de runtime del ordenamiento de credenciales.js sin navegador:
   stub mínimo de DOM que ejecuta el listener DOMContentLoaded con una tabla
   fake y verifica el reordenamiento visual (appendChild = mover al final).
   Uso: node scripts/smoke_credenciales_js.js */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// filas en el ORDEN BASE del servidor (activos, admins, alfabético).
const filas = [
  { cells: [{ textContent: "cuenta000" }, { textContent: "a@x.com" }, { textContent: "Ana" }, { textContent: "Panel" }, { textContent: "Deshabilitada" }, { textContent: "01/03/2026 10:00" }], dataset: { username: "cuenta000", email: "a@x.com" } },
  { cells: [{ textContent: "cuenta001" }, { textContent: "b@x.com" }, { textContent: "" }, { textContent: "Chat" }, { textContent: "Activa" }, { textContent: "nunca" }], dataset: { username: "cuenta001", email: "b@x.com" } },
  { cells: [{ textContent: "cuenta002" }, { textContent: "c@x.com" }, { textContent: "" }, { textContent: "Admin" }, { textContent: "Activa" }, { textContent: "02/03/2026 09:30" }], dataset: { username: "cuenta002", email: "c@x.com" } },
];

// appendChild = mover la fila al final (efecto visual real del reorden).
const tbody = {
  querySelectorAll: () => filas.slice(),
  appendChild: (f) => {
    filas.splice(filas.indexOf(f), 1);
    filas.push(f);
  },
};

const botones = ["usuario", "rol", "ultimo_login"].map((col) => {
  const btn = {
    dataset: { sort: col },
    classList: { toggle() {} },
    closest() {
      return { setAttribute() {} };
    },
    querySelector() {
      return { classList: { toggle() {} } };
    },
    _click: null,
  };
  btn.addEventListener = (_ev, cb) => {
    btn._click = cb;
  };
  return btn;
});

const tabla = { querySelector: () => tbody, querySelectorAll: () => botones };

const doc = {
  body: { dataset: { userId: "7" } },
  getElementById: (id) => (id === "tabla-cuentas" ? tabla : null),
  querySelectorAll: () => [],
  addEventListener: (_ev, cb) => {
    doc._domReady = cb;
  },
};

const ctx = { document: doc, console, Number, Date, Array };
vm.createContext(ctx);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "django_metrics", "static", "js", "credenciales.js"), "utf8"),
  ctx
);
doc._domReady();

function assert(cond, msg) {
  if (!cond) {
    console.error("FALLO:", msg);
    process.exit(1);
  }
}
const orden = () => filas.map((f) => f.dataset.username).join(",");

// Click usuario -> asc
botones[0]._click();
assert(orden() === "cuenta000,cuenta001,cuenta002", "usuario asc: " + orden());
// Click usuario de nuevo -> desc
botones[0]._click();
assert(orden() === "cuenta002,cuenta001,cuenta000", "usuario desc: " + orden());
// Click rol -> asc por jerarquía de badges (Admin=1, Panel=2, Chat=3)
botones[1]._click();
assert(orden() === "cuenta002,cuenta000,cuenta001", "rol asc (Admin,Panel,Chat): " + orden());
// Click ultimo_login -> asc: recientes al final, 'nunca' SIEMPRE al final
botones[2]._click();
assert(orden() === "cuenta000,cuenta002,cuenta001", "fecha asc con 'nunca' al final: " + orden());
// Click ultimo_login de nuevo -> desc: recientes primero, 'nunca' sigue al final
botones[2]._click();
assert(orden() === "cuenta002,cuenta000,cuenta001", "fecha desc con 'nunca' al final: " + orden());
console.log("OK - ordenamiento verificado (asc/desc, rol por badge, 'nunca' al final)");
