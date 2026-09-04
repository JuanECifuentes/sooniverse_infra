/**
 * Módulo Credenciales: modal de modificación y modal de confirmación
 * (deshabilitar/habilitar). Mismo criterio que worker-actions.js: <dialog>
 * compartidos, NUNCA window.confirm(); el relleno del modal sale de los
 * data-* de la fila clickeada (sin AJAX: el POST es un submit normal con
 * recarga, que conserva filtros/página vía el campo 'next').
 */
document.addEventListener("DOMContentLoaded", () => {
  const dlgEditar = document.getElementById("credencial-editar-dialog");
  const dlgEstado = document.getElementById("credencial-estado-dialog");

  document.querySelectorAll("[data-cerrar-dialog]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const d = document.getElementById(btn.dataset.cerrarDialog);
      if (d) d.close();
    });
  });

  if (dlgEditar) {
    const form = dlgEditar.querySelector("form");
    const urlTemplate = dlgEditar.dataset.urlTemplate || "";
    const userActual = String(document.body.dataset.userId ?? "");

    document.querySelectorAll("button[data-editar]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const fila = btn.closest("tr");
        if (!fila) return;
        const d = fila.dataset;
        if (urlTemplate) form.action = urlTemplate.replace(/\/0\//, `/${d.id}/`);
        const titulo = dlgEditar.querySelector("#editar-titulo");
        if (titulo) titulo.textContent = d.username;
        const set = (id, v) => {
          const el = dlgEditar.querySelector(`#${id}`);
          if (el) el.value = v ?? "";
        };
        // IDs reales del modal: el form usa auto_id="ed_%s" -> ed_<campo>.
        // Todo se rellena con lo ya configurado SALVO las contraseñas.
        set("ed_email", d.email);
        set("ed_first_name", d.first);
        set("ed_last_name", d.last);
        set("ed_password", "");
        set("ed_password2", "");
        // Red de seguridad cliente-side del autobloqueo: el servidor también
        // fuerza disabled en su propio POST (usa initial, ignora lo forzado).
        const esPropia = String(d.id) === userActual;
        const cbStaff = dlgEditar.querySelector("#ed_is_staff");
        const cbAdmin = dlgEditar.querySelector("#ed_es_admin");
        if (cbStaff) {
          cbStaff.checked = d.staff === "1";
          cbStaff.disabled = esPropia;
        }
        if (cbAdmin) {
          cbAdmin.checked = d.admin === "1";
          cbAdmin.disabled = esPropia;
        }
        dlgEditar.showModal();
      });
    });

    // Reapertura tras un POST con errores: el modal llega con el bound form.
    if (dlgEditar.dataset.autoopen) dlgEditar.showModal();
  }

  if (dlgEstado) {
    const form = dlgEstado.querySelector("form");
    const mensaje = dlgEstado.querySelector("#estado-mensaje");
    const confirmar = dlgEstado.querySelector("#estado-confirmar");
    const urlTemplate = dlgEstado.dataset.urlTemplate || "";

    document.querySelectorAll("button[data-estado]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const fila = btn.closest("tr");
        if (!fila) return;
        const d = fila.dataset;
        const accion = btn.dataset.estado;
        if (urlTemplate) form.action = urlTemplate.replace(/\/0\//, `/${d.id}/`);
        const inputAccion = form.querySelector('[name="accion"]');
        if (inputAccion) inputAccion.value = accion;
        if (mensaje) {
          mensaje.textContent =
            accion === "deshabilitar"
              ? `¿Deshabilitar a '${d.username}'? Perderá el acceso al chat y al panel hasta que lo habilites de nuevo.`
              : `¿Habilitar de nuevo a '${d.username}'? Recuperará el acceso según su rol.`;
        }
        if (confirmar) {
          confirmar.className = `sv-btn sv-btn--${
            accion === "deshabilitar" ? "danger" : "secondary"
          } sv-btn--sm`;
          confirmar.textContent = accion === "deshabilitar" ? "Deshabilitar" : "Habilitar";
        }
        dlgEstado.showModal();
      });
    });
  }

  /* ==========================================================================
   * Ordenamiento de columnas 100% CLIENTE
   * ==========================================================================
   * Sin queryparams y sin recargas: al clickear un encabezado se reordenan
   * VISUALMENTE las filas ya renderizadas (la página visible de 30). El
   * servidor solo aporta el orden base (activos, admins, alfabético); el
   * orden clickeado no se persiste en la URL.
   */
  const tabla = document.getElementById("tabla-cuentas");
  if (tabla) {
    const tbody = tabla.querySelector("tbody");
    const botones = Array.from(tabla.querySelectorAll("th button[data-sort]"));

    // Estado del ordenamiento activo (columna + dirección).
    let colActiva = null;
    let dirAsc = true;

    // Misma jerarquía que las badges de la columna ROL (views.py::_rol_de).
    const ROL_RANK = { superuser: 0, admin: 1, panel: 2, chat: 3 };

    const textoCelda = (fila, indice) =>
      (fila.cells[indice]?.textContent || "").trim();

    // 'd/m/Y H:i' -> timestamp; 'nunca' -> Infinity (siempre al final).
    const fechaDe = (texto) => {
      const m = texto.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4}) (\d{1,2}):(\d{2})$/);
      if (!m) return Infinity;
      return new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5]).getTime();
    };

    const claveDe = (fila, col) => {
      switch (col) {
        case "usuario":
          return (fila.dataset.username || "").toLowerCase();
        case "correo":
          return (fila.dataset.email || "").toLowerCase();
        case "nombre":
          return textoCelda(fila, 2).toLowerCase();
        case "rol":
          return ROL_RANK[textoCelda(fila, 3).toLowerCase()] ?? 9;
        case "estado":
          return textoCelda(fila, 4).includes("Deshabilitada") ? 1 : 0;
        case "ultimo_login":
          return fechaDe(textoCelda(fila, 5));
        default:
          return "";
      }
    };

    function aplicar(col, asc) {
      const filas = Array.from(tbody.querySelectorAll("tr"));
      filas.sort((a, b) => {
        const va = claveDe(a, col);
        const vb = claveDe(b, col);
        if (col === "ultimo_login") {
          // 'nunca' queda al final invierta o no la dirección.
          const finA = !Number.isFinite(va);
          const finB = !Number.isFinite(vb);
          if (finA || finB) return finA === finB ? 0 : finA ? 1 : -1;
        }
        if (va === vb) return 0;
        const menor = va < vb;
        return (asc && menor) || (!asc && !menor) ? -1 : 1;
      });
      filas.forEach((fila) => tbody.appendChild(fila));

      // Indicadores visuales: chevron y aria-sort solo en la columna activa.
      botones.forEach((btn) => {
        const th = btn.closest("th");
        const activo = btn.dataset.sort === col;
        btn.classList.toggle("sv-sort--activa", activo);
        btn
          .querySelector(".sv-sort__flecha")
          ?.classList.toggle("sv-sort__flecha--desc", activo && !asc);
        th.setAttribute("aria-sort", activo ? (asc ? "ascending" : "descending") : "none");
      });
    }

    botones.forEach((btn) => {
      btn.addEventListener("click", () => {
        const col = btn.dataset.sort;
        const asc = colActiva === col ? !dirAsc : true;
        colActiva = col;
        dirAsc = asc;
        aplicar(col, asc);
      });
    });
  }
});
