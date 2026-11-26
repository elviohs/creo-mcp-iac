#define _CRT_SECURE_NO_WARNINGS
/*
  creo_iac_tk - programa auxiliar de creo-mcp-iac (Departamento de Ingenieria Mecanica, IAC)

  Se conecta a la sesion de Creo Parametric ya abierta (modo asincrono simple de
  Creo Toolkit en C), ejecuta UNA orden de LECTURA y se desconecta. Escribe el
  resultado en JSON (UTF-8) por la salida estandar. No modifica ni guarda modelos.

  Uso:  creo_iac_tk.exe <orden> [--modelo NOMBRE.EXT] [--rapido]
  Ordenes:
     estado          Version de Creo y modelo activo.
     interferencias  Interferencias globales del conjunto (analisis nativo de Creo).
     taladros        Operaciones de taladro y roscas cosmeticas de la pieza o de
                     todas las piezas del conjunto (arbol de elementos + ejes).
     posiciones      Componentes del conjunto con su ruta, su matriz de posicion y, en las
                     piezas, su material y su caja (coordenadas de la pieza).
     holguras --pares F
                     Distancia minima (ProFitClearanceCompute) entre los pares de componentes
                     del fichero F (una linea por par: "ruta_a ruta_b", rutas con ids separados
                     por '/'), con los puntos mas cercanos en coordenadas del conjunto.
     caras           Caras planas y planos de referencia de una pieza (para colocar operaciones).
     seleccion       Lo que el usuario tiene seleccionado en Creo.
     aristas         Aristas de una pieza (caras vecinas, longitud, concava/convexa).
     arbol [--ids L] Operaciones de la pieza (id, nombre, tipo, estado, patron) o, con --ids,
                     su arbol de elementos y el de su patron.
   Escritura (solo si el servicio la permite; nunca guarda):
     crear --plan F  Crea UNA operacion a partir de un plan (arbol de elementos) y
                     comprueba que el eje queda donde se pidio.
     borrar --ids L  Borra operaciones creadas por el asistente (nombre IA_*).
  Por defecto se usa el modelo activo; --modelo elige otro modelo ya en sesion.
*/
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include <stdlib.h>
#include <math.h>
#include "ProToolkit.h"
#include "ProCore.h"
#include "ProMdl.h"
#include "ProUtil.h"
#include "ProSolid.h"
#include "ProFeature.h"
#include "ProFeatType.h"
#include "ProAsmcomp.h"
#include "ProAsmcomppath.h"
#include "ProFit.h"
#include "ProSelection.h"
#include "ProModelitem.h"
#include "ProArray.h"
#include "ProElement.h"
#include "ProElemId.h"
#include "ProValue.h"
#include "ProHole.h"
#include "ProGeomitem.h"
#include "ProGeomitemdata.h"
#include "ProMdlUnits.h"
#include "ProReference.h"
#include "ProSelbuffer.h"
#include "ProSurface.h"
#include "ProSurfacedata.h"
#include "ProFeatType.h"
#include "ProSolidBody.h"
#include "ProPoint.h"
#include "ProPattern.h"
#include "ProEdge.h"
#include "ProContour.h"
#include "ProCrvcollection.h"
#include "ProCollect.h"
#include "ProMaterial.h"
#include "ProElempath.h"
#include "ProSection.h"
#include "ProSecdim.h"
#include "ProSecdimType.h"
#include "ProSecerror.h"
#include "Pro2dEntdef.h"
#include <time.h>
#include "salida.h"

#define VERSION_TK "0.9.0"
#define MAX_MODELOS 20000

static ProProcessHandle sesion;
static const char *orden_actual = "";

/* ------------------------------------------------------------------ util */

static const char *tipo_mdl(ProMdlType t)
{
    switch (t) {
    case PRO_MDL_ASSEMBLY: return "ASM";
    case PRO_MDL_PART:     return "PRT";
    case PRO_MDL_DRAWING:  return "DRW";
    default:               return "OTRO";
    }
}

static void error_json(const char *fase, int codigo, const char *texto)
{
    j_abrir_obj();
    j_bool("ok", 0);
    j_str("version_tk", VERSION_TK);
    j_str("orden", orden_actual);
    j_str("fase", fase);
    j_int("codigo", codigo);
    j_str("error", texto);
    j_cerrar_obj();
    j_fin();
}

static void cabecera_ok(void)
{
    j_abrir_obj();
    j_bool("ok", 1);
    j_str("version_tk", VERSION_TK);
    j_str("orden", orden_actual);
}

static void modelo_json(const char *clave, ProMdl mdl)
{
    ProMdlName nombre;
    ProMdlType tipo = PRO_MDL_UNUSED;
    nombre[0] = L'\0';
    ProMdlMdlnameGet(mdl, nombre);
    ProMdlTypeGet(mdl, &tipo);
    j_clave_obj(clave);
    j_wstr("nombre", nombre);
    j_str("tipo", tipo_mdl(tipo));
    j_cerrar_obj();
}

static void unidad_longitud_json(ProMdl mdl)
{
    ProUnitsystem sistema;
    ProUnititem unidad;
    if (ProMdlPrincipalunitsystemGet(mdl, &sistema) == PRO_TK_NO_ERROR &&
        ProUnitsystemUnitGet(&sistema, PRO_UNITTYPE_LENGTH, &unidad) == PRO_TK_NO_ERROR)
        j_wstr("unidad_longitud", unidad.name);
    else
        j_null("unidad_longitud");
}

static int conectar(void)
{
    ProBoolean al_azar = PRO_B_FALSE;
    ProError e = ProEngineerConnect("", "", "", "", PRO_B_FALSE, 20, &al_azar, &sesion);
    if (e != PRO_TK_NO_ERROR) {
        const char *t = "No se pudo conectar con Creo";
        if (e == PRO_TK_E_AMBIGUOUS) t = "Hay mas de una sesion de Creo abierta; deja solo una";
        else if (e == PRO_TK_E_NOT_FOUND || e == PRO_TK_NOT_EXIST) t = "No se encuentra ninguna sesion de Creo abierta";
        else if (e == PRO_TK_CANT_MODIFY) t = "Falta la variable PRO_COMM_MSG_EXE (ruta de pro_comm_msg.exe)";
        else if (e == PRO_TK_E_BUSY) t = "Creo no ha respondido a tiempo (puede estar ocupado)";
        error_json("conexion", (int)e, t);
        return 0;
    }
    return 1;
}

/* Modelo de trabajo: --modelo NOMBRE.EXT (en sesion) o el modelo activo */
static int modelo_objetivo(const char *nombre, ProMdl *mdl)
{
    ProError e;
    if (nombre && *nombre) {
        ProMdlName wn;
        ProMdlfileType ft = PRO_MDLFILE_PART;
        char base[PRO_MDLNAME_SIZE];
        const char *punto = strrchr(nombre, '.');
        size_t n = punto ? (size_t)(punto - nombre) : strlen(nombre);
        if (n >= sizeof(base)) n = sizeof(base) - 1;
        memcpy(base, nombre, n); base[n] = '\0';
        if (punto && (_stricmp(punto, ".asm") == 0)) ft = PRO_MDLFILE_ASSEMBLY;
        ProStringToWstring(wn, base);
        e = ProMdlnameInit(wn, ft, mdl);
        if (e != PRO_TK_NO_ERROR) {
            error_json("modelo", (int)e, "El modelo indicado no esta en la sesion de Creo");
            return 0;
        }
        return 1;
    }
    e = ProMdlCurrentGet(mdl);
    if (e != PRO_TK_NO_ERROR) {
        error_json("modelo", (int)e, "No hay modelo activo en Creo");
        return 0;
    }
    return 1;
}

/* ------------------------------------------------- recorrido del conjunto */

typedef void (*AccionComp)(ProMdl hijo, ProAsmcomppath *ruta, void *dato);

typedef struct {
    ProSolid raiz;
    int ids[PRO_MAX_ASSEM_LEVEL];
    int n;
    AccionComp accion;
    void *dato;
} Recorrido;

static ProError filtro_comp(ProFeature *f, ProAppData d)
{
    ProFeattype t;
    ProFeatStatus st;
    if (ProFeatureTypeGet(f, &t) != PRO_TK_NO_ERROR || t != PRO_FEAT_COMPONENT)
        return PRO_TK_CONTINUE;
    if (ProFeatureStatusGet(f, &st) == PRO_TK_NO_ERROR && st != PRO_FEAT_ACTIVE)
        return PRO_TK_CONTINUE;
    return PRO_TK_NO_ERROR;
}

static ProError visita_comp(ProFeature *f, ProError status, ProAppData d)
{
    Recorrido *r = (Recorrido *)d;
    ProMdl hijo;
    ProMdlType t;
    ProAsmcomppath ruta;
    if (ProAsmcompMdlGet((ProAsmcomp *)f, &hijo) != PRO_TK_NO_ERROR || hijo == NULL)
        return PRO_TK_NO_ERROR;
    if (r->n >= PRO_MAX_ASSEM_LEVEL) return PRO_TK_NO_ERROR;
    r->ids[r->n++] = f->id;
    if (ProAsmcomppathInit(r->raiz, r->ids, r->n, &ruta) == PRO_TK_NO_ERROR)
        r->accion(hijo, &ruta, r->dato);
    if (ProMdlTypeGet(hijo, &t) == PRO_TK_NO_ERROR && t == PRO_MDL_ASSEMBLY)
        ProSolidFeatVisit((ProSolid)hijo, visita_comp, filtro_comp, d);
    r->n--;
    return PRO_TK_NO_ERROR;
}

static void recorrer_conjunto(ProMdl raiz, AccionComp accion, void *dato)
{
    Recorrido r;
    memset(&r, 0, sizeof(r));
    r.raiz = (ProSolid)raiz;
    r.accion = accion;
    r.dato = dato;
    ProSolidFeatVisit((ProSolid)raiz, visita_comp, filtro_comp, &r);
}

static void ruta_json(const char *clave, ProAsmcomppath *ruta)
{
    int i;
    j_abrir_lista(clave);
    for (i = 0; i < ruta->table_num; i++) j_int(NULL, ruta->comp_id_table[i]);
    j_cerrar_lista();
}

/* ----------------------------------------------------------------- estado */

static int orden_estado(void)
{
    int version = 0;
    ProMdl mdl;
    ProToolkitMajorVersionGet(&version);
    cabecera_ok();
    j_int("creo_version_mayor", version);
    if (ProMdlCurrentGet(&mdl) == PRO_TK_NO_ERROR) modelo_json("modelo_activo", mdl);
    else j_null("modelo_activo");
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ---------------------------------------------------------- interferencias */

static void seleccion_json(const char *clave, ProSelection s)
{
    ProModelitem it;
    ProAsmcomppath ruta;
    j_clave_obj(clave);
    if (ProSelectionModelitemGet(s, &it) == PRO_TK_NO_ERROR && it.owner != NULL) {
        ProMdlName n;
        n[0] = L'\0';
        ProMdlMdlnameGet(it.owner, n);
        j_wstr("modelo", n);
        j_int("tipo_elemento", (long)it.type);
        j_int("id_elemento", (long)it.id);
    }
    if (ProSelectionAsmcomppathGet(s, &ruta) == PRO_TK_NO_ERROR)
        ruta_json("ruta", &ruta);
    j_cerrar_obj();
}

static int orden_interferencias(const char *modelo, int rapido)
{
    ProMdl mdl;
    ProMdlType tipo;
    ProInterferenceInfo *info = NULL;
    ProError e;
    int n = 0, i;

    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    if (tipo != PRO_MDL_ASSEMBLY) {
        error_json("modelo", 0, "El modelo no es un conjunto");
        return 4;
    }
    e = ProFitGlobalinterferenceCompute((ProAssembly)mdl, PRO_FIT_PART, PRO_B_FALSE,
                                        PRO_B_FALSE, rapido ? PRO_B_TRUE : PRO_B_FALSE, &info);
    if (e != PRO_TK_NO_ERROR) {
        error_json("calculo", (int)e, "Creo no pudo calcular las interferencias");
        return 5;
    }
    if (info != NULL) ProArraySizeGet((ProArray)info, &n);

    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_bool("rapido", rapido);
    j_int("total", n);
    j_abrir_lista("interferencias");
    for (i = 0; i < n; i++) {
        double vol = 0.0;
        j_abrir_obj();
        seleccion_json("a", info[i].part_1);
        seleccion_json("b", info[i].part_2);
        if (!rapido && info[i].interf_data != NULL &&
            ProFitInterferencevolumeCompute(info[i].interf_data, &vol) == PRO_TK_NO_ERROR)
            j_num("volumen", vol);
        else
            j_null("volumen");
        j_cerrar_obj();
    }
    j_cerrar_lista();
    j_cerrar_obj();
    j_fin();
    if (info != NULL) ProInterferenceInfoProarrayFree(info);
    return 0;
}

/* -------------------------------------------------------------- taladros */

typedef struct {
    ProMdl modelos[MAX_MODELOS];
    int cantidad[MAX_MODELOS];
    int n;
} ListaModelos;

static void anadir_modelo(ListaModelos *l, ProMdl m)
{
    int i;
    for (i = 0; i < l->n; i++)
        if (l->modelos[i] == m) { l->cantidad[i]++; return; }
    if (l->n < MAX_MODELOS) { l->modelos[l->n] = m; l->cantidad[l->n] = 1; l->n++; }
}

static void accion_lista(ProMdl hijo, ProAsmcomppath *ruta, void *dato)
{
    anadir_modelo((ListaModelos *)dato, hijo);
}

/* Vuelca los valores escalares del arbol de elementos: clave = ruta de ids */
static int indices_array = 0;   /* 1: los hijos de un array llevan [i] en la ruta (orden arbol) */

static void volcar_elemento(ProElement e, const char *ruta, int prof)
{
    ProElemId id = 0;
    ProBoolean es = PRO_B_FALSE;
    ProValueDataType vt;
    char r2[256];

    if (prof > 12) return;
    ProElementIdGet(e, &id);
    if (ruta[0]) _snprintf(r2, sizeof(r2), "%s.%d", ruta, (int)id);
    else _snprintf(r2, sizeof(r2), "%d", (int)id);
    r2[sizeof(r2) - 1] = '\0';

    if (ProElementIsCompound(e, NULL, &es) == PRO_TK_NO_ERROR && es) {
        ProElement *hijos = NULL;
        int n = 0, i;
        if (ProArrayAlloc(0, sizeof(ProElement), 1, (ProArray *)&hijos) == PRO_TK_NO_ERROR) {
            if (ProElementChildrenGet(e, NULL, &hijos) == PRO_TK_NO_ERROR) {
                ProArraySizeGet((ProArray)hijos, &n);
                for (i = 0; i < n; i++) volcar_elemento(hijos[i], r2, prof + 1);
            }
            ProArrayFree((ProArray *)&hijos);
        }
        return;
    }
    es = PRO_B_FALSE;
    if (ProElementIsArray(e, NULL, &es) == PRO_TK_NO_ERROR && es) {
        ProElement *hijos = NULL;
        int n = 0, i;
        if (ProArrayAlloc(0, sizeof(ProElement), 1, (ProArray *)&hijos) == PRO_TK_NO_ERROR) {
            if (ProElementArrayGet(e, NULL, &hijos) == PRO_TK_NO_ERROR) {
                ProArraySizeGet((ProArray)hijos, &n);
                for (i = 0; i < n; i++) {
                    if (indices_array) {
                        char r3[272];
                        _snprintf(r3, sizeof(r3), "%s[%d]", r2, i);
                        r3[sizeof(r3) - 1] = '\0';
                        volcar_elemento(hijos[i], r3, prof + 1);
                    } else {
                        volcar_elemento(hijos[i], r2, prof + 1);
                    }
                }
            }
            ProArrayFree((ProArray *)&hijos);
        }
        return;
    }
    if (ProElementValuetypeGet(e, &vt) != PRO_TK_NO_ERROR) return;
    switch (vt) {
    case PRO_VALUE_TYPE_INT: {
        int v;
        if (ProElementIntegerGet(e, NULL, &v) == PRO_TK_NO_ERROR) j_int(r2, v);
        break; }
    case PRO_VALUE_TYPE_DOUBLE: {
        double v;
        if (ProElementDoubleGet(e, NULL, &v) == PRO_TK_NO_ERROR) j_num(r2, v);
        break; }
    case PRO_VALUE_TYPE_BOOLEAN: {
        ProBoolean v;
        if (ProElementBooleanGet(e, NULL, &v) == PRO_TK_NO_ERROR) j_bool(r2, v == PRO_B_TRUE);
        break; }
    case PRO_VALUE_TYPE_WSTRING: {
        wchar_t *v = NULL;
        if (ProElementWstringGet(e, NULL, &v) == PRO_TK_NO_ERROR && v) { j_wstr(r2, v); ProWstringFree(v); }
        break; }
    case PRO_VALUE_TYPE_SELECTION: {
        ProReference ref = NULL;
        if (ProElementReferenceGet(e, NULL, &ref) == PRO_TK_NO_ERROR && ref != NULL) {
            ProType rt = PRO_TYPE_UNUSED;
            int rid = -1;
            char txt[64];
            ProReferenceTypeGet(ref, &rt);
            ProReferenceIdGet(ref, &rid);
            _snprintf(txt, sizeof(txt), "ref:%d:%d", (int)rt, rid);
            txt[sizeof(txt) - 1] = '\0';
            j_str(r2, txt);
            ProReferenceFree(ref);
        }
        break; }
    default:
        break;
    }
}

static ProError visita_eje(ProGeomitem *it, ProError status, ProAppData d)
{
    ProGeomitemdata *gd = NULL;
    if (ProGeomitemdataGet(it, &gd) == PRO_TK_NO_ERROR && gd != NULL) {
        if (gd->data.p_curve_data != NULL) {
            ProLinedata *l = &gd->data.p_curve_data->line;
            j_abrir_lista(NULL);
            j_num(NULL, l->end1[0]); j_num(NULL, l->end1[1]); j_num(NULL, l->end1[2]);
            j_num(NULL, l->end2[0]); j_num(NULL, l->end2[1]); j_num(NULL, l->end2[2]);
            j_cerrar_lista();
        }
        ProGeomitemdataFree(&gd);
    }
    return PRO_TK_NO_ERROR;
}

typedef struct { int suprimidas; int n; } Cuenta;

static ProError filtro_taladro(ProFeature *f, ProAppData d)
{
    ProFeattype t;
    ProBoolean visible = PRO_B_TRUE;
    if (ProFeatureTypeGet(f, &t) != PRO_TK_NO_ERROR) return PRO_TK_CONTINUE;
    if (t != PRO_FEAT_HOLE && t != PRO_FEAT_THREAD) return PRO_TK_CONTINUE;
    if (ProFeatureVisibilityGet(f, &visible) == PRO_TK_NO_ERROR && !visible) return PRO_TK_CONTINUE;
    return PRO_TK_NO_ERROR;
}

static ProError visita_taladro(ProFeature *f, ProError status, ProAppData d)
{
    Cuenta *c = (Cuenta *)d;
    ProFeattype t;
    ProFeatStatus st = PRO_FEAT_ACTIVE;
    ProName nombre;
    ProElement arbol = NULL;

    ProFeatureStatusGet(f, &st);
    if (st != PRO_FEAT_ACTIVE) { c->suprimidas++; return PRO_TK_NO_ERROR; }
    ProFeatureTypeGet(f, &t);
    c->n++;

    j_abrir_obj();
    j_int("id", f->id);
    nombre[0] = L'\0';
    if (ProModelitemNameGet((ProModelitem *)f, nombre) == PRO_TK_NO_ERROR) j_wstr("nombre", nombre);
    j_str("clase", t == PRO_FEAT_HOLE ? "taladro" : "rosca_cosmetica");

    if (ProFeatureElemtreeExtract(f, NULL, PRO_FEAT_EXTRACT_NO_OPTS, &arbol) == PRO_TK_NO_ERROR && arbol) {
        if (t == PRO_FEAT_HOLE) {
            wchar_t *w = NULL;
            if (ProElementHoleThreadSeriesGet(arbol, f->owner, &w) == PRO_TK_NO_ERROR && w) {
                j_wstr("serie_rosca", w); ProWstringFree(w); w = NULL;
            }
            if (ProElementHoleScrewSizeGet(arbol, f->owner, &w) == PRO_TK_NO_ERROR && w) {
                j_wstr("tamano_tornillo", w); ProWstringFree(w); w = NULL;
            }
        }
        j_clave_obj("elementos");
        volcar_elemento(arbol, "", 0);
        j_cerrar_obj();
        ProFeatureElemtreeFree(f, arbol);
    } else {
        j_null("elementos");
    }
    j_abrir_lista("ejes");
    ProFeatureGeomitemVisit(f, PRO_AXIS, visita_eje, NULL, NULL);
    j_cerrar_lista();
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static ListaModelos lista;

static int orden_taladros(const char *modelo)
{
    ProMdl mdl;
    ProMdlType tipo;
    int i;

    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    memset(&lista, 0, sizeof(lista));
    anadir_modelo(&lista, mdl);
    if (tipo == PRO_MDL_ASSEMBLY) recorrer_conjunto(mdl, accion_lista, &lista);

    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_abrir_lista("modelos");
    for (i = 0; i < lista.n; i++) {
        ProMdlName n;
        ProMdlType t = PRO_MDL_UNUSED;
        Cuenta c = {0, 0};
        n[0] = L'\0';
        ProMdlMdlnameGet(lista.modelos[i], n);
        ProMdlTypeGet(lista.modelos[i], &t);
        j_abrir_obj();
        j_wstr("nombre", n);
        j_str("tipo", tipo_mdl(t));
        j_int("instancias", i == 0 ? 1 : lista.cantidad[i]);
        unidad_longitud_json(lista.modelos[i]);
        j_abrir_lista("operaciones");
        ProSolidFeatVisit((ProSolid)lista.modelos[i], visita_taladro, filtro_taladro, &c);
        j_cerrar_lista();
        j_int("suprimidas", c.suprimidas);
        j_cerrar_obj();
    }
    j_cerrar_lista();
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ------------------------------------------------------------ posiciones */

static void accion_posicion(ProMdl hijo, ProAsmcomppath *ruta, void *dato)
{
    ProMatrix m;
    ProMdlName n;
    ProMdlType t = PRO_MDL_UNUSED;
    int i, k;
    n[0] = L'\0';
    ProMdlMdlnameGet(hijo, n);
    ProMdlTypeGet(hijo, &t);
    j_abrir_obj();
    j_wstr("modelo", n);
    j_str("tipo", tipo_mdl(t));
    ruta_json("ruta", ruta);
    if (ProAsmcomppathTrfGet(ruta, PRO_B_TRUE, m) == PRO_TK_NO_ERROR) {
        j_abrir_lista("matriz");
        for (i = 0; i < 4; i++)
            for (k = 0; k < 4; k++) j_num(NULL, m[i][k]);
        j_cerrar_lista();
    } else {
        j_null("matriz");
    }
    if (t == PRO_MDL_PART) {
        ProMaterial mat;
        Pro3dPnt caja[2];
        if (ProMaterialCurrentGet((ProSolid)hijo, &mat) == PRO_TK_NO_ERROR) j_wstr("material", mat.matl_name);
        else j_null("material");
        if (ProSolidOutlineGet((ProSolid)hijo, caja) == PRO_TK_NO_ERROR) {
            j_abrir_lista("caja");
            for (i = 0; i < 2; i++)
                for (k = 0; k < 3; k++) j_num(NULL, caja[i][k]);
            j_cerrar_lista();
        } else {
            j_null("caja");
        }
    }
    j_cerrar_obj();
}

static int orden_posiciones(const char *modelo)
{
    ProMdl mdl;
    ProMdlType tipo;
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    if (tipo != PRO_MDL_ASSEMBLY) {
        error_json("modelo", 0, "El modelo no es un conjunto");
        return 4;
    }
    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_str("matriz_formato", "4x4 por filas; filas 0-2 ejes X,Y,Z del componente, fila 3 origen (coordenadas del conjunto)");
    j_abrir_lista("componentes");
    recorrer_conjunto(mdl, accion_posicion, NULL);
    j_cerrar_lista();
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* -------------------------------------------------------------- holguras */

#define LIMITE_HOLGURAS_S 540.0

static int leer_ruta(const char **p, int *ids, int *n)
{
    char *fin;
    long v;
    *n = 0;
    while (**p == ' ') (*p)++;
    for (;;) {
        v = strtol(*p, &fin, 10);
        if (fin == *p || v <= 0 || *n >= PRO_MAX_ASSEM_LEVEL) return 0;
        ids[(*n)++] = (int)v;
        *p = fin;
        if (**p != '/') break;
        (*p)++;
    }
    return *n > 0;
}

static ProError seleccion_comp(ProMdl raiz, int *ids, int n, ProSelection *sel, ProAsmcomppath *ruta,
                               ProMdl *mdl)
{
    ProModelitem it;
    ProError e = ProAsmcomppathInit((ProSolid)raiz, ids, n, ruta);
    if (e == PRO_TK_NO_ERROR) e = ProAsmcomppathMdlGet(ruta, mdl);
    if (e == PRO_TK_NO_ERROR) e = ProMdlToModelitem(*mdl, &it);
    if (e == PRO_TK_NO_ERROR) e = ProSelectionAlloc(ruta, &it, sel);
    return e;
}

/* Si el punto (coordenadas del conjunto) esta en una cara cilindrica de la pieza, escribe su
   radio y su eje en coordenadas del conjunto (para los ajustes concentricos en frio). */
static void cilindro_json(const char *clave, ProAsmcomppath *ruta, ProMdl mdl, const double *p)
{
    ProMatrix m;
    Pro3dPnt local;
    ProSurface srf;
    ProUvParam uv;
    ProSrftype tipo;
    ProGeomitemdata *gd = NULL;
    int j, k;
    if (ProAsmcomppathTrfGet(ruta, PRO_B_TRUE, m) != PRO_TK_NO_ERROR) return;
    for (k = 0; k < 3; k++) {                  /* inversa de una matriz de giro + traslacion */
        local[k] = 0.0;
        for (j = 0; j < 3; j++) local[k] += (p[j] - m[3][j]) * m[k][j];
    }
    if (ProSolidProjectPoint((ProSolid)mdl, local, 0.01, &srf, uv) != PRO_TK_NO_ERROR) return;
    if (ProSurfaceTypeGet(srf, &tipo) != PRO_TK_NO_ERROR || tipo != PRO_SRF_CYL) return;
    if (ProSurfaceDataGet(srf, &gd) != PRO_TK_NO_ERROR || gd == NULL || gd->data.p_surface_data == NULL) return;
    {
        ProCylinderdata *c = &gd->data.p_surface_data->srf_shape.cylinder;
        double o[3], d[3];
        for (k = 0; k < 3; k++) {
            o[k] = m[3][k]; d[k] = 0.0;
            for (j = 0; j < 3; j++) { o[k] += c->origin[j] * m[j][k]; d[k] += c->e3[j] * m[j][k]; }
        }
        j_clave_obj(clave);
        j_num("radio", c->radius);
        j_abrir_lista("eje");
        for (k = 0; k < 3; k++) j_num(NULL, o[k]);
        for (k = 0; k < 3; k++) j_num(NULL, d[k]);
        j_cerrar_lista();
        j_cerrar_obj();
    }
    ProGeomitemdataFree(&gd);
}

static int orden_holguras(const char *modelo, const char *fichero)
{
    ProMdl mdl;
    ProMdlType tipo;
    FILE *f;
    char linea[600];
    int n = 0, fallidos = 0, truncado = 0, i;
    time_t t0 = time(NULL);

    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    if (tipo != PRO_MDL_ASSEMBLY) {
        error_json("modelo", 0, "El modelo no es un conjunto");
        return 4;
    }
    if (fichero == NULL || (f = fopen(fichero, "r")) == NULL) {
        error_json("argumentos", -1, "Falta el fichero de pares (--pares)");
        return 2;
    }
    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_abrir_lista("pares");
    while (fgets(linea, sizeof(linea), f) != NULL) {
        int ida[PRO_MAX_ASSEM_LEVEL], idb[PRO_MAX_ASSEM_LEVEL], na = 0, nb = 0;
        const char *p = linea;
        ProSelection sa = NULL, sb = NULL;
        double dist = 0.0, proy = 0.0;
        ProBoolean interf = PRO_B_FALSE;
        int superficies = 0;
        Pro3dPnt pts[2];
        ProAsmcomppath rutaa, rutab;
        ProMdl mdla = NULL, mdlb = NULL;
        ProError e;
        if (!leer_ruta(&p, ida, &na) || !leer_ruta(&p, idb, &nb)) continue;
        if (difftime(time(NULL), t0) > LIMITE_HOLGURAS_S) { truncado = 1; break; }
        e = seleccion_comp(mdl, ida, na, &sa, &rutaa, &mdla);
        if (e == PRO_TK_NO_ERROR) e = seleccion_comp(mdl, idb, nb, &sb, &rutab, &mdlb);
        if (e == PRO_TK_NO_ERROR) {
            e = ProFitClearanceCompute(sa, PRO_FIT_INCLUDE_NONE, sb, PRO_FIT_INCLUDE_NONE, PRO_FIT_WHOLE_SURFACE,
                                       NULL, PRO_B_FALSE, &dist, &proy, &interf, pts);
            if (e == PRO_TK_BAD_INPUTS)   /* por si no admite referencia de proyeccion nula */
                e = ProFitClearanceCompute(sa, PRO_FIT_INCLUDE_NONE, sb, PRO_FIT_INCLUDE_NONE,
                                           PRO_FIT_WHOLE_SURFACE, sa, PRO_B_FALSE, &dist, &proy, &interf, pts);
            if (e == PRO_TK_GENERAL_ERROR) {
                /* una de las piezas no tiene solido (p. ej. superficies importadas de un
                   fabricante): se repite incluyendo las superficies */
                e = ProFitClearanceCompute(sa, PRO_FIT_INCLUDE_FROM_QUILTS, sb, PRO_FIT_INCLUDE_TO_QUILTS,
                                           PRO_FIT_WHOLE_SURFACE, NULL, PRO_B_FALSE, &dist, &proy, &interf, pts);
                if (e == PRO_TK_GENERAL_ERROR)   /* geometria importada como facetas (malla) */
                    e = ProFitClearanceCompute(sa, PRO_FIT_INCLUDE_FROM_FACETS_N_QUILTS, sb,
                                               PRO_FIT_INCLUDE_TO_FACETS_N_QUILTS, PRO_FIT_WHOLE_SURFACE, NULL,
                                               PRO_B_FALSE, &dist, &proy, &interf, pts);
                if (e == PRO_TK_NO_ERROR) superficies = 1;
            }
        }
        j_abrir_obj();
        j_int("n", n);
        if (e == PRO_TK_NO_ERROR) {
            j_num("distancia", dist);
            j_bool("interferencia", interf == PRO_B_TRUE);
            if (superficies) j_bool("superficies", 1);
            j_abrir_lista("puntos");
            for (i = 0; i < 2; i++) {
                j_abrir_lista(NULL);
                j_num(NULL, pts[i][0]); j_num(NULL, pts[i][1]); j_num(NULL, pts[i][2]);
                j_cerrar_lista();
            }
            j_cerrar_lista();
            if (interf != PRO_B_TRUE && dist > 1e-6) {
                cilindro_json("cilindro_a", &rutaa, mdla, pts[0]);
                cilindro_json("cilindro_b", &rutab, mdlb, pts[1]);
            }
        } else {
            j_int("error", (long)e);
            fallidos++;
        }
        j_cerrar_obj();
        if (sa) ProSelectionFree(&sa);
        if (sb) ProSelectionFree(&sb);
        n++;
    }
    fclose(f);
    j_cerrar_lista();
    j_int("calculados", n);
    j_int("fallidos", fallidos);
    j_bool("truncado", truncado);
    j_num("segundos", difftime(time(NULL), t0));
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ----------------------------------------------------------------- caras */

static void vec_json(const char *clave, const double *v)
{
    j_abrir_lista(clave);
    j_num(NULL, v[0]); j_num(NULL, v[1]); j_num(NULL, v[2]);
    j_cerrar_lista();
}

static ProError filtro_plana(ProSurface s, ProAppData d)
{
    ProSrftype t;
    if (ProSurfaceTypeGet(s, &t) != PRO_TK_NO_ERROR || t != PRO_SRF_PLANE) return PRO_TK_CONTINUE;
    return PRO_TK_NO_ERROR;
}

static void cara_campos(ProSurface s)
{
    int id = -1;
    double area = 0.0;
    ProGeomitemdata *gd = NULL;
    ProSurfaceIdGet(s, &id);
    ProSurfaceAreaEval(s, &area);
    j_int("id", id);
    j_num("area", area);
    if (ProSurfaceDataGet(s, &gd) == PRO_TK_NO_ERROR && gd != NULL && gd->data.p_surface_data != NULL) {
        ProSurfacedata *sd = gd->data.p_surface_data;
        ProUvParam uv;
        ProVector xyz, normal, d1[2], d2[3];
        uv[0] = 0.5 * (sd->uv_min[0] + sd->uv_max[0]);
        uv[1] = 0.5 * (sd->uv_min[1] + sd->uv_max[1]);
        if (ProSurfaceXyzdataEval(s, uv, xyz, d1, d2, normal) == PRO_TK_NO_ERROR) {
            vec_json("normal_exterior", normal);
            vec_json("punto_plano", xyz);
        }
        vec_json("caja_min", sd->xyz_min);
        vec_json("caja_max", sd->xyz_max);
        ProGeomitemdataFree(&gd);
    }
}

static ProError visita_cara(ProSurface s, ProError st, ProAppData d)
{
    j_abrir_obj();
    cara_campos(s);
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static ProError visita_plano_dtm(ProGeomitem *it, ProError st, ProAppData d)
{
    ProGeomitemdata *gd = NULL;
    ProFeature *f = (ProFeature *)d;
    ProName nombre;
    nombre[0] = L'\0';
    ProModelitemNameGet((ProModelitem *)f, nombre);
    j_abrir_obj();
    j_wstr("nombre", nombre);
    j_int("id", it->id);
    j_int("id_operacion", f->id);
    if (ProGeomitemdataGet(it, &gd) == PRO_TK_NO_ERROR && gd != NULL && gd->data.p_surface_data != NULL) {
        ProSurfacedata *sd = gd->data.p_surface_data;
        if (sd->type == PRO_SRF_PLANE) {
            vec_json("normal", sd->srf_shape.plane.e3);
            vec_json("origen", sd->srf_shape.plane.origin);
        }
        ProGeomitemdataFree(&gd);
    }
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static ProError filtro_dtm(ProFeature *f, ProAppData d)
{
    ProFeattype t;
    ProFeatStatus st = PRO_FEAT_ACTIVE;
    if (ProFeatureTypeGet(f, &t) != PRO_TK_NO_ERROR || t != PRO_FEAT_DATUM) return PRO_TK_CONTINUE;
    if (ProFeatureStatusGet(f, &st) == PRO_TK_NO_ERROR && st != PRO_FEAT_ACTIVE) return PRO_TK_CONTINUE;
    return PRO_TK_NO_ERROR;
}

static ProError visita_dtm(ProFeature *f, ProError status, ProAppData d)
{
    ProFeatureGeomitemVisit(f, PRO_SURFACE, visita_plano_dtm, NULL, (ProAppData)f);
    return PRO_TK_NO_ERROR;
}

static ProError visita_eje_ref(ProGeomitem *it, ProError st, ProAppData d)
{
    ProGeomitemdata *gd = NULL;
    ProFeature *f = (ProFeature *)d;
    ProName nombre;
    nombre[0] = L'\0';
    ProModelitemNameGet((ProModelitem *)f, nombre);
    j_abrir_obj();
    j_wstr("nombre", nombre);
    j_int("id", it->id);
    j_int("id_operacion", f->id);
    if (ProGeomitemdataGet(it, &gd) == PRO_TK_NO_ERROR && gd != NULL && gd->data.p_curve_data != NULL) {
        double dvec[3];
        int k;
        ProLinedata *l = &gd->data.p_curve_data->line;
        for (k = 0; k < 3; k++) dvec[k] = l->end2[k] - l->end1[k];
        vec_json("punto", l->end1);
        vec_json("direccion", dvec);
        ProGeomitemdataFree(&gd);
    }
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static ProError filtro_eje_dtm(ProFeature *f, ProAppData d)
{
    ProFeattype t;
    ProFeatStatus st = PRO_FEAT_ACTIVE;
    if (ProFeatureTypeGet(f, &t) != PRO_TK_NO_ERROR || t != PRO_FEAT_DATUM_AXIS) return PRO_TK_CONTINUE;
    if (ProFeatureStatusGet(f, &st) == PRO_TK_NO_ERROR && st != PRO_FEAT_ACTIVE) return PRO_TK_CONTINUE;
    return PRO_TK_NO_ERROR;
}

static ProError visita_eje_dtm(ProFeature *f, ProError status, ProAppData d)
{
    ProFeatureGeomitemVisit(f, PRO_AXIS, visita_eje_ref, NULL, (ProAppData)f);
    return PRO_TK_NO_ERROR;
}

static int orden_caras(const char *modelo)
{
    ProMdl mdl;
    ProMdlType tipo;
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    if (tipo != PRO_MDL_PART) {
        error_json("modelo", 0, "El modelo no es una pieza");
        return 4;
    }
    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_abrir_lista("caras_planas");
    ProSolidSurfaceVisit((ProSolid)mdl, visita_cara, filtro_plana, NULL);
    j_cerrar_lista();
    j_abrir_lista("planos_referencia");
    ProSolidFeatVisit((ProSolid)mdl, visita_dtm, filtro_dtm, NULL);
    j_cerrar_lista();
    j_abrir_lista("ejes_referencia");
    ProSolidFeatVisit((ProSolid)mdl, visita_eje_dtm, filtro_eje_dtm, NULL);
    j_cerrar_lista();
    {
        ProSolidBody cuerpo;
        if (ProSolidDefaultBodyGet((ProSolid)mdl, &cuerpo) == PRO_TK_NO_ERROR) j_int("cuerpo_por_defecto", cuerpo.id);
        else j_null("cuerpo_por_defecto");
    }
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* -------------------------------------------------------------- seleccion */

static int orden_seleccion(void)
{
    ProSelection *sels = NULL;
    int n = 0, i;
    ProError e = ProSelbufferSelectionsGet(&sels);
    cabecera_ok();
    j_abrir_lista("seleccion");
    if (e == PRO_TK_NO_ERROR && sels != NULL) {
        ProArraySizeGet((ProArray)sels, &n);
        for (i = 0; i < n; i++) {
            ProModelitem it;
            ProAsmcomppath ruta;
            j_abrir_obj();
            if (ProSelectionModelitemGet(sels[i], &it) == PRO_TK_NO_ERROR) {
                ProMdlName nm;
                ProMdlType mt = PRO_MDL_UNUSED;
                nm[0] = L'\0';
                if (it.owner) { ProMdlMdlnameGet(it.owner, nm); ProMdlTypeGet(it.owner, &mt); }
                j_int("tipo_elemento", (long)it.type);
                j_int("id", it.id);
                j_wstr("modelo", nm);
                j_str("tipo_modelo", tipo_mdl(mt));
                if (it.type == PRO_SURFACE && it.owner) {
                    ProSurface srf;
                    if (ProSurfaceInit(it.owner, it.id, &srf) == PRO_TK_NO_ERROR) {
                        j_clave_obj("cara");
                        cara_campos(srf);
                        j_cerrar_obj();
                    }
                }
                if (it.type == PRO_POINT && it.owner) {
                    ProPoint pt;
                    ProVector xyz;
                    if (ProPointInit((ProSolid)it.owner, it.id, &pt) == PRO_TK_NO_ERROR &&
                        ProPointCoordGet(pt, xyz) == PRO_TK_NO_ERROR)
                        vec_json("coordenadas", xyz);
                }
            }
            {
                /* punto donde se pincho, en coordenadas de la pieza seleccionada */
                ProPoint3d p3;
                if (ProSelectionPoint3dGet(sels[i], p3) == PRO_TK_NO_ERROR) vec_json("punto", p3);
            }
            if (ProSelectionAsmcomppathGet(sels[i], &ruta) == PRO_TK_NO_ERROR && ruta.table_num > 0)
                ruta_json("ruta", &ruta);
            j_cerrar_obj();
        }
        ProSelectionarrayFree(sels);
    }
    j_cerrar_lista();
    j_int("total", n);
    if (e != PRO_TK_NO_ERROR) j_int("codigo_seleccion", (int)e);
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ---------------------------------------------------------------- aristas */
/* Aristas de la pieza con sus dos caras, longitud, extremos y si son concavas (esquina
   interior, p. ej. la de una cajera) o convexas. Solo lectura. */

typedef struct { ProSolid sld; int *vistas; int n, cap; ProSurface cara; } VisAristas;

static void normal_en(ProSolid sld, ProSurface s, const double *p, double *n)
{
    ProUvParam uv;
    ProVector xyz, d1[2], d2[3], nn;
    int k;
    for (k = 0; k < 3; k++) n[k] = 0;
    if (ProSurfaceParamEval(sld, s, (double *)p, uv) == PRO_TK_NO_ERROR &&
        ProSurfaceXyzdataEval(s, uv, xyz, d1, d2, nn) == PRO_TK_NO_ERROR)
        for (k = 0; k < 3; k++) n[k] = nn[k];
}

static ProError visita_arista(ProEdge ed, ProError st, ProAppData d)
{
    VisAristas *va = (VisAristas *)d;
    int id = -1, k, dir1 = 1, id1 = -1, id2 = -1;
    ProEnttype tipo = PRO_ENT_NONE;
    ProEdge e1, e2;
    ProSurface s1 = NULL, s2 = NULL;
    double lon = 0, n1[3], n2[3], t1[3], in1[3], nt, punto;
    ProVector a, b, m, der1, der2, dirv;
    ProEdgeIdGet(ed, &id);
    for (k = 0; k < va->n; k++) if (va->vistas[k] == id) return PRO_TK_NO_ERROR;
    if (va->n == va->cap) {
        int *nv = (int *)realloc(va->vistas, sizeof(int) * (va->cap * 2 + 64));
        if (!nv) return PRO_TK_NO_ERROR;
        va->vistas = nv; va->cap = va->cap * 2 + 64;
    }
    va->vistas[va->n++] = id;
    ProEdgeTypeGet(ed, &tipo);
    ProEdgeLengthEval(ed, &lon);
    ProEdgeXyzdataEval(ed, 0.0, a, der1, der2, dirv);
    ProEdgeXyzdataEval(ed, 1.0, b, der1, der2, dirv);
    ProEdgeXyzdataEval(ed, 0.5, m, der1, der2, dirv);
    j_abrir_obj();
    j_int("id", id);
    j_str("tipo", tipo == PRO_ENT_LINE ? "recta" : tipo == PRO_ENT_ARC ? "arco" : "otra");
    j_num("longitud", lon);
    vec_json("inicio", a);
    vec_json("fin", b);
    vec_json("medio", m);
    nt = sqrt(der1[0] * der1[0] + der1[1] * der1[1] + der1[2] * der1[2]);
    if (nt > 1e-12) { double tu[3]; for (k = 0; k < 3; k++) tu[k] = der1[k] / nt; vec_json("tangente", tu); }
    if (ProEdgeNeighborsGet(ed, &e1, &e2, &s1, &s2) == PRO_TK_NO_ERROR && s1 && s2) {
        ProSurfaceIdGet(s1, &id1);
        ProSurfaceIdGet(s2, &id2);
        j_abrir_lista("caras"); j_int(NULL, id1); j_int(NULL, id2); j_cerrar_lista();
        normal_en(va->sld, s1, m, n1);
        normal_en(va->sld, s2, m, n2);
        ProEdgeDirGet(ed, s1, &dir1);
        if (nt > 1e-12) {
            for (k = 0; k < 3; k++) t1[k] = dir1 * der1[k] / nt;
            /* n1 x t1 apunta hacia FUERA de la cara 1 con el sentido que da ProEdgeDirGet
               (comprobado en Creo 12.4 el 2026-11-24: aristas exteriores de un bloque y
               esquinas interiores de un corte) */
            in1[0] = n1[1] * t1[2] - n1[2] * t1[1];
            in1[1] = n1[2] * t1[0] - n1[0] * t1[2];
            in1[2] = n1[0] * t1[1] - n1[1] * t1[0];
            punto = n2[0] * in1[0] + n2[1] * in1[1] + n2[2] * in1[2];
            j_num("cos_normales", n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]);
            if (fabs(n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]) > 0.9999) j_str("forma", "tangente");
            else j_str("forma", punto < 0 ? "concava" : "convexa");
            j_int("sentido_cara1", dir1);
        }
    }
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static ProError visita_contorno_ar(ProContour c, ProError st, ProAppData d)
{
    VisAristas *va = (VisAristas *)d;
    ProContourEdgeVisit(va->cara, c, visita_arista, NULL, d);
    return PRO_TK_NO_ERROR;
}

static ProError visita_cara_ar(ProSurface s, ProError st, ProAppData d)
{
    VisAristas *va = (VisAristas *)d;
    va->cara = s;
    ProSurfaceContourVisit(s, visita_contorno_ar, NULL, d);
    return PRO_TK_NO_ERROR;
}

static int orden_aristas(const char *modelo)
{
    ProMdl mdl;
    ProMdlType tipo;
    VisAristas va;
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    ProMdlTypeGet(mdl, &tipo);
    if (tipo != PRO_MDL_PART) { error_json("modelo", 0, "El modelo no es una pieza"); return 4; }
    memset(&va, 0, sizeof(va));
    va.sld = (ProSolid)mdl;
    cabecera_ok();
    modelo_json("modelo", mdl);
    unidad_longitud_json(mdl);
    j_abrir_lista("aristas");
    ProSolidSurfaceVisit((ProSolid)mdl, visita_cara_ar, NULL, &va);
    j_cerrar_lista();
    j_int("total", va.n);
    j_cerrar_obj();
    j_fin();
    free(va.vistas);
    return 0;
}

/* ------------------------------------------------------------------ arbol */
/* Lista las operaciones de una pieza (id, nombre, tipo, estado, patron) o vuelca el arbol de
   elementos de las indicadas (y el del patron si son guia de uno). Solo lectura. */

static ProError visita_op_lista(ProFeature *f, ProError status, ProAppData d)
{
    ProFeattype t = 0;
    ProFeatStatus st = PRO_FEAT_ACTIVE;
    ProPatternStatus ps = PRO_PATTERN_NONE;
    ProBoolean visible = PRO_B_TRUE;
    ProName nombre;
    if (ProFeatureVisibilityGet(f, &visible) == PRO_TK_NO_ERROR && !visible) return PRO_TK_NO_ERROR;
    ProFeatureTypeGet(f, &t);
    ProFeatureStatusGet(f, &st);
    ProFeaturePatternStatusGet(f, &ps);
    j_abrir_obj();
    j_int("id", f->id);
    nombre[0] = L'\0';
    if (ProModelitemNameGet((ProModelitem *)f, nombre) == PRO_TK_NO_ERROR) j_wstr("nombre", nombre);
    j_int("tipo", (long)t);
    j_int("estado", (long)st);
    j_int("patron", (long)ps);
    j_cerrar_obj();
    return PRO_TK_NO_ERROR;
}

static int orden_arbol(const char *modelo, const char *ids_txt)
{
    ProMdl mdl;
    char copia[512], *tok;
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    cabecera_ok();
    modelo_json("modelo", mdl);
    if (!ids_txt || !*ids_txt) {
        j_abrir_lista("operaciones");
        ProSolidFeatVisit((ProSolid)mdl, visita_op_lista, NULL, NULL);
        j_cerrar_lista();
    } else {
        indices_array = 1;
        strncpy(copia, ids_txt, sizeof(copia) - 1); copia[sizeof(copia) - 1] = '\0';
        j_abrir_lista("arboles");
        for (tok = strtok(copia, ","); tok; tok = strtok(NULL, ",")) {
            ProFeature fe;
            ProElement arbol = NULL;
            ProPatternStatus ps = PRO_PATTERN_NONE;
            ProFeattype t = 0;
            ProError e;
            int id = atoi(tok);
            j_abrir_obj();
            j_int("id", id);
            if (ProModelitemInit(mdl, id, PRO_FEATURE, (ProModelitem *)&fe) != PRO_TK_NO_ERROR) {
                j_str("error", "no existe"); j_cerrar_obj(); continue;
            }
            ProFeatureTypeGet(&fe, &t);
            ProFeaturePatternStatusGet(&fe, &ps);
            j_int("tipo", (long)t);
            j_int("patron", (long)ps);
            e = ProFeatureElemtreeExtract(&fe, NULL, PRO_FEAT_EXTRACT_NO_OPTS, &arbol);
            j_int("extraer", (long)e);
            if (e == PRO_TK_NO_ERROR && arbol) {
                j_clave_obj("elementos");
                volcar_elemento(arbol, "", 0);
                j_cerrar_obj();
                ProFeatureElemtreeFree(&fe, arbol);
                arbol = NULL;
            }
            if (ps != PRO_PATTERN_NONE) {
                e = ProPatternElemtreeCreate(&fe, PRO_FEAT_PATTERN, &arbol);
                j_int("patron_extraer", (long)e);
                if (e == PRO_TK_NO_ERROR && arbol) {
                    j_clave_obj("patron_elementos");
                    volcar_elemento(arbol, "", 0);
                    j_cerrar_obj();
                    ProElementFree(&arbol);
                }
            }
            j_cerrar_obj();
        }
        j_cerrar_lista();
        indices_array = 0;
    }
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ------------------------------------------------------------ crear (plan) */

#define MAX_ELEM 256
typedef struct { char ruta[160]; ProElement el; } Nodo;
static Nodo nodos[MAX_ELEM];
static int n_nodos = 0;

static ProElement buscar_nodo(const char *ruta)
{
    int i;
    for (i = 0; i < n_nodos; i++) if (strcmp(nodos[i].ruta, ruta) == 0) return nodos[i].el;
    return NULL;
}

/* Crea (si no existe) el elemento de la ruta "386.1671.1672" y sus compuestos padre. */
static ProElement nodo_ruta(const char *ruta, ProElement *raiz)
{
    char padre[160];
    const char *punto;
    ProElement el, p;
    int id;
    if ((el = buscar_nodo(ruta)) != NULL) return el;
    punto = strrchr(ruta, '.');
    id = atoi(punto ? punto + 1 : ruta);
    if (ProElementAlloc((ProElemId)id, &el) != PRO_TK_NO_ERROR) return NULL;
    if (!punto) {
        *raiz = el;
    } else {
        size_t n = (size_t)(punto - ruta);
        if (n >= sizeof(padre)) return NULL;
        memcpy(padre, ruta, n); padre[n] = '\0';
        p = nodo_ruta(padre, raiz);
        if (p == NULL || ProElemtreeElementAdd(p, NULL, el) != PRO_TK_NO_ERROR) return NULL;
    }
    if (n_nodos < MAX_ELEM) {
        strncpy(nodos[n_nodos].ruta, ruta, sizeof(nodos[n_nodos].ruta) - 1);
        nodos[n_nodos].ruta[sizeof(nodos[n_nodos].ruta) - 1] = '\0';
        nodos[n_nodos].el = el;
        n_nodos++;
    }
    return el;
}

static void quitar_fin(char *t)
{
    size_t n = strlen(t);
    while (n > 0 && (t[n - 1] == '\n' || t[n - 1] == '\r' || t[n - 1] == ' ')) t[--n] = '\0';
}

/* Distancia de un punto a la recta (a,b) y paralelismo con la direccion d */
static void comprobar_eje(const double *a, const double *b, const double *p, const double *d,
                          double *dist, double *coseno)
{
    double u[3], w[3], c[3], nu, nd;
    int k;
    for (k = 0; k < 3; k++) { u[k] = b[k] - a[k]; w[k] = p[k] - a[k]; }
    nu = sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]);
    nd = sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
    if (nu < 1e-12) { *dist = 1e30; *coseno = 0; return; }
    c[0] = u[1] * w[2] - u[2] * w[1];
    c[1] = u[2] * w[0] - u[0] * w[2];
    c[2] = u[0] * w[1] - u[1] * w[0];
    *dist = sqrt(c[0] * c[0] + c[1] * c[1] + c[2] * c[2]) / nu;
    *coseno = nd > 1e-12 ? fabs((u[0] * d[0] + u[1] * d[1] + u[2] * d[2]) / (nu * nd)) : 1.0;
}

typedef struct { double a[3], b[3]; int hay; } EjeLeido;

static ProError visita_eje_creado(ProGeomitem *it, ProError status, ProAppData d)
{
    EjeLeido *e = (EjeLeido *)d;
    ProGeomitemdata *gd = NULL;
    if (e->hay) return PRO_TK_NO_ERROR;
    if (ProGeomitemdataGet(it, &gd) == PRO_TK_NO_ERROR && gd != NULL && gd->data.p_curve_data != NULL) {
        int k;
        for (k = 0; k < 3; k++) { e->a[k] = gd->data.p_curve_data->line.end1[k]; e->b[k] = gd->data.p_curve_data->line.end2[k]; }
        e->hay = 1;
        ProGeomitemdataFree(&gd);
    }
    return PRO_TK_NO_ERROR;
}

static ProError borrar_ids(ProSolid sld, int *ids, int n)
{
    int *lista = NULL;
    ProFeatureDeleteOptions *op = NULL;
    ProError e;
    int i;
    ProArrayAlloc(0, sizeof(int), 1, (ProArray *)&lista);
    for (i = 0; i < n; i++) ProArrayObjectAdd((ProArray *)&lista, PRO_VALUE_UNUSED, 1, &ids[i]);
    ProArrayAlloc(1, sizeof(ProFeatureDeleteOptions), 1, (ProArray *)&op);
    op[0] = PRO_FEAT_DELETE_CLIP;
    e = ProFeatureWithoptionsDelete(sld, lista, op, PRO_REGEN_NO_RESOLVE_MODE);
    ProArrayFree((ProArray *)&op);
    ProArrayFree((ProArray *)&lista);
    return e;
}

/* ---- patrones: crear --plan con «patron <id_guia>», «esperado x y z» y «alternar_i r1 r2» ---- */
#define MAX_ESPERADOS 200
static double esperados[MAX_ESPERADOS][3];
static int n_esperados = 0;

typedef struct { double a[MAX_ESPERADOS + 1][6]; int id[MAX_ESPERADOS + 1]; int n; } EjesMiembros;

static ProError visita_miembro(ProFeature *f, ProError st, ProAppData d)
{
    EjesMiembros *em = (EjesMiembros *)d;
    EjeLeido eje;
    int k;
    if (em->n > MAX_ESPERADOS) return PRO_TK_NO_ERROR;
    memset(&eje, 0, sizeof(eje));
    ProFeatureGeomitemVisit(f, PRO_AXIS, visita_eje_creado, NULL, &eje);
    em->id[em->n] = f->id;
    for (k = 0; k < 3; k++) { em->a[em->n][k] = eje.hay ? eje.a[k] : 1e30; em->a[em->n][3 + k] = eje.hay ? eje.b[k] : 1e30; }
    em->n++;
    return PRO_TK_NO_ERROR;
}

/* Cada punto esperado tiene un eje de miembro que pasa por el (0,01 mm) y paralelo a la direccion. */
static int patron_correcto(EjesMiembros *em, const double *dir, double *peor)
{
    int i, j;
    *peor = 0;
    if (em->n != n_esperados) return 0;
    for (i = 0; i < n_esperados; i++) {
        double mejor = 1e30;
        for (j = 0; j < em->n; j++) {
            double dist, cosv;
            if (em->a[j][0] > 1e29) continue;
            comprobar_eje(em->a[j], em->a[j] + 3, esperados[i], dir, &dist, &cosv);
            if (cosv >= 0.9999 && dist < mejor) mejor = dist;
        }
        if (mejor > *peor) *peor = mejor;
        if (mejor > 0.01) return 0;
    }
    return 1;
}

static int crear_patron(ProMdl mdl, ProElement raiz, int id_guia, const double *dir, const char *ai1, const char *ai2)
{
    ProFeature guia;
    ProElement e1 = ai1[0] ? buscar_nodo(ai1) : NULL, e2 = ai2[0] ? buscar_nodo(ai2) : NULL;
    int v1 = 0, v2 = 0, intento, max_intentos = (n_esperados > 0 && (e1 || e2)) ? 4 : 1;
    static const int alt[4][2] = {{0, 0}, {1, 0}, {0, 1}, {1, 1}};
    ProError ultimo = PRO_TK_GENERAL_ERROR;
    double peor = -1;
    ProPatternStatus ps = PRO_PATTERN_NONE;

    if (ProModelitemInit(mdl, id_guia, PRO_FEATURE, (ProModelitem *)&guia) != PRO_TK_NO_ERROR) {
        error_json("plan", id_guia, "La operacion guia del patron no existe"); return 2;
    }
    ProFeaturePatternStatusGet(&guia, &ps);
    if (ps != PRO_PATTERN_NONE) {
        error_json("plan", id_guia, "La operacion guia ya forma parte de un patron"); return 2;
    }
    if (e1) ProElementIntegerGet(e1, NULL, &v1);
    if (e2) ProElementIntegerGet(e2, NULL, &v2);
    for (intento = 0; intento < max_intentos; intento++) {
        ProPattern pat;
        ProFeature cab;
        EjesMiembros *em;
        if (e1) ProElementIntegerSet(e1, v1 ^ alt[intento][0]);
        if (e2) ProElementIntegerSet(e2, v2 ^ alt[intento][1]);
        ultimo = ProPatternCreate(&guia, PRO_FEAT_PATTERN, raiz);
        if (ultimo != PRO_TK_NO_ERROR) continue;
        if (ProFeaturePatternGet(&guia, PRO_FEAT_PATTERN, &pat) != PRO_TK_NO_ERROR) { ultimo = PRO_TK_GENERAL_ERROR; continue; }
        em = (EjesMiembros *)calloc(1, sizeof(EjesMiembros));
        if (!em) { ProPatternDelete(&pat); error_json("memoria", 0, "Sin memoria"); return 5; }
        ProPatternMemberVisit(&pat, visita_miembro, NULL, em);
        /* la guia no siempre esta entre los miembros visitados */
        {
            int k, esta = 0;
            for (k = 0; k < em->n; k++) if (em->id[k] == guia.id) esta = 1;
            if (!esta) visita_miembro(&guia, PRO_TK_NO_ERROR, em);
        }
        if (n_esperados > 0 && !patron_correcto(em, dir, &peor)) {
            ProPatternDelete(&pat);
            free(em);
            ultimo = PRO_TK_GENERAL_ERROR;
            continue;
        }
        ProPatternHeaderGet(&pat, &cab);
        {
            ProName nombre;
            int k;
            nombre[0] = L'\0';
            ProModelitemNameGet((ProModelitem *)&cab, nombre);
            cabecera_ok();
            modelo_json("modelo", mdl);
            j_clave_obj("operacion");
            j_int("id", cab.id);
            j_wstr("nombre", nombre);
            j_int("guia", guia.id);
            j_int("intentos", intento + 1);
            j_abrir_lista("alternado"); j_int(NULL, alt[intento][0]); j_int(NULL, alt[intento][1]); j_cerrar_lista();
            if (n_esperados > 0) j_num("desviacion", peor);
            j_abrir_lista("miembros");
            for (k = 0; k < em->n; k++) {
                j_abrir_obj();
                j_int("id", em->id[k]);
                if (em->a[k][0] < 1e29) {
                    j_abrir_lista("eje");
                    j_num(NULL, em->a[k][0]); j_num(NULL, em->a[k][1]); j_num(NULL, em->a[k][2]);
                    j_num(NULL, em->a[k][3]); j_num(NULL, em->a[k][4]); j_num(NULL, em->a[k][5]);
                    j_cerrar_lista();
                }
                j_cerrar_obj();
            }
            j_cerrar_lista();
            j_cerrar_obj();
            j_bool("guardado", 0);
            j_cerrar_obj();
            j_fin();
        }
        free(em);
        ProElementFree(&raiz);
        return 0;
    }
    ProElementFree(&raiz);
    if (n_esperados > 0 && peor >= 0)
        error_json("verificacion", (int)(peor * 1000),
                   "El patron no quedaba donde se pedia con ningun sentido de las direcciones: no se ha creado");
    else
        error_json("creacion", (int)ultimo, "Creo no ha podido crear el patron (revisa el plan); no se ha creado nada");
    return 5;
}

/* -------------------------------------------- extrusiones (crear con croquis) */

#ifndef PI
#define PI 3.14159265358979323846
#endif
#define MAX_SK 400
typedef struct { int tipo; double v[12]; } EntSk;   /* 1 linea, 2 arco, 3 circulo, 4 linea de centros */
static EntSk sk_ent[MAX_SK];
static int n_sk = 0;
static int sk_refs[8][2];
static int n_sk_refs = 0;

/* Cotas del boceto (0.8.0): se crean como cotas fuertes antes del acotado automatico. */
#define MAX_COTAS 200
typedef struct { int tipo, ent, ent2, ref, pt; double valor, lugar[3]; } CotaSk;
/* tipo: 1 diametro, 2 radio, 3 punto a referencia, 4 recta a referencia, 5 entre dos rectas,
   6 diametro respecto a la linea de centros (revoluciones; ent2 = linea de centros, pt 'w' = recta entera) */
static CotaSk sk_cotas[MAX_COTAS];
static int n_cotas = 0;
static int sk_ids[MAX_SK];
static int sk_proj[8];
typedef struct { int tipo; double valor; int propia; } CotaRes;
static CotaRes cotas_res[MAX_COTAS];
static int n_cotas_res = 0, cotas_fallidas = 0, cotas_propias = 0;
static const char *cotas_estado = "automaticas";
static double desviacion_sk = -1;

/* Punto del modelo -> coordenadas 2D de la seccion (matriz seccion -> modelo, por filas). */
static void a_seccion(ProMatrix m, const double *p, double *uv)
{
    int k;
    double d[3], n0 = 0, n1 = 0;
    for (k = 0; k < 3; k++) { d[k] = p[k] - m[3][k]; n0 += m[0][k] * m[0][k]; n1 += m[1][k] * m[1][k]; }
    uv[0] = (d[0] * m[0][0] + d[1] * m[0][1] + d[2] * m[0][2]) / (n0 > 0 ? n0 : 1);
    uv[1] = (d[0] * m[1][0] + d[1] * m[1][1] + d[2] * m[1][2]) / (n1 > 0 ? n1 : 1);
}

static double volumen_pieza(ProMdl mdl)
{
    ProMassProperty mp;
    memset(&mp, 0, sizeof(mp));
    if (ProSolidMassPropertyGet((ProSolid)mdl, NULL, &mp) != PRO_TK_NO_ERROR) return -1.0;
    return mp.volume;
}

/* Mensajes de error del croquis (se anaden al texto del error) */
static char sk_err_txt[700];
static void errores_seccion_json(ProWSecerror *err)
{
    int n = 0, i;
    ProMsg msg;
    char u8[300];
    if (ProSecerrorCount(err, &n) != PRO_TK_NO_ERROR || n <= 0) return;
    for (i = 0; i < n && i < 5; i++) {
        msg[0] = L'\0';
        ProSecerrorMsgGet(*err, i, msg);
        u8[0] = '\0';
        a_utf8(msg, u8, (int)sizeof(u8));
        if (strlen(sk_err_txt) + strlen(u8) + 4 < sizeof(sk_err_txt)) {
            if (sk_err_txt[0]) strcat(sk_err_txt, " | ");
            strcat(sk_err_txt, u8);
        }
    }
}

/* Rellena la seccion de la operacion incompleta: proyecta las referencias, anade las
   entidades (en coordenadas del modelo), acota automaticamente y resuelve. */
static ProError construir_seccion(ProMdl mdl, ProSection sec, const char **fase)
{
    ProMatrix m;
    ProWSecerror err = NULL;
    ProError e;
    int i, id, own[MAX_COTAS], n_own = 0;
    *fase = "situacion";
    if ((e = ProSectionLocationGet(sec, m)) != PRO_TK_NO_ERROR) return e;
    *fase = "referencias";
    for (i = 0; i < n_sk_refs; i++) {
        ProModelitem it;
        ProSelection sel = NULL;
        if (ProModelitemInit(mdl, sk_refs[i][1], (ProType)sk_refs[i][0], &it) != PRO_TK_NO_ERROR ||
            ProSelectionAlloc(NULL, &it, &sel) != PRO_TK_NO_ERROR) return PRO_TK_BAD_INPUTS;
        e = ProSectionEntityFromProjection(sec, sel, &id);
        ProSelectionFree(&sel);
        if (e != PRO_TK_NO_ERROR) return e;
        sk_proj[i] = id;
    }
    *fase = "entidades";
    for (i = 0; i < n_sk; i++) {
        EntSk *x = &sk_ent[i];
        if (x->tipo == 1) {
            Pro2dLinedef l;
            l.type = PRO_2D_LINE;
            a_seccion(m, x->v, l.end1);
            a_seccion(m, x->v + 3, l.end2);
            e = ProSectionEntityAdd(sec, (Pro2dEntdef *)&l, &id);
        } else if (x->tipo == 2) {
            Pro2dArcdef a;
            double c[2], s2[2], e2[2], m2[2], a1, a2, am, r;
            a_seccion(m, x->v, c); a_seccion(m, x->v + 3, s2); a_seccion(m, x->v + 6, e2); a_seccion(m, x->v + 9, m2);
            r = sqrt((s2[0] - c[0]) * (s2[0] - c[0]) + (s2[1] - c[1]) * (s2[1] - c[1]));
            a1 = atan2(s2[1] - c[1], s2[0] - c[0]);
            a2 = atan2(e2[1] - c[1], e2[0] - c[0]);
            am = atan2(m2[1] - c[1], m2[0] - c[0]);
            /* sentido antihorario de a1 a a2; si el punto medio no esta en ese tramo, al reves */
            while (a2 <= a1) a2 += 2 * PI;
            while (am < a1) am += 2 * PI;
            if (am > a2) { double t = a1; a1 = a2 - 2 * PI; a2 = t; while (a2 <= a1) a2 += 2 * PI; }
            a.type = PRO_2D_ARC;
            a.center[0] = c[0]; a.center[1] = c[1];
            a.start_angle = a1; a.end_angle = a2; a.radius = r;
            e = ProSectionEntityAdd(sec, (Pro2dEntdef *)&a, &id);
        } else if (x->tipo == 4) {
            Pro2dClinedef cl;
            cl.type = PRO_2D_CENTER_LINE;
            a_seccion(m, x->v, cl.end1);
            a_seccion(m, x->v + 3, cl.end2);
            e = ProSectionEntityAdd(sec, (Pro2dEntdef *)&cl, &id);
        } else {
            Pro2dCircledef ci;
            ci.type = PRO_2D_CIRCLE;
            a_seccion(m, x->v, ci.center);
            ci.radius = x->v[3];
            e = ProSectionEntityAdd(sec, (Pro2dEntdef *)&ci, &id);
        }
        if (e != PRO_TK_NO_ERROR) return e;
        sk_ids[i] = id;
    }
    /* Cotas del boceto como cotas fuertes; el acotado automatico solo anade las que falten. */
    n_own = 0;
    cotas_fallidas = 0;
    cotas_estado = n_cotas > 0 ? "boceto" : "automaticas";
    *fase = "cotas";
    for (i = 0; i < n_cotas && i < MAX_COTAS; i++) {
        CotaSk *c = &sk_cotas[i];
        int ents[2];
        ProSectionPointType pts[2];
        int n = 0, did = -1;
        ProSecdimType tipo;
        Pro2dPnt lugar;
        if (c->ent < 0 || c->ent >= n_sk || ((c->tipo == 5 || c->tipo == 6) && (c->ent2 < 0 || c->ent2 >= n_sk)) ||
            ((c->tipo == 3 || c->tipo == 4) && (c->ref < 0 || c->ref >= n_sk_refs))) { cotas_fallidas++; continue; }
        a_seccion(m, c->lugar, lugar);
        if (c->tipo == 1 || c->tipo == 2) {
            ents[0] = sk_ids[c->ent]; pts[0] = PRO_ENT_WHOLE; n = 1;
            tipo = c->tipo == 1 ? PRO_TK_DIM_DIA : PRO_TK_DIM_RAD;
        } else if (c->tipo == 3) {
            ents[0] = sk_proj[c->ref]; pts[0] = PRO_ENT_WHOLE;
            ents[1] = sk_ids[c->ent]; pts[1] = c->pt == 'c' ? PRO_ENT_CENTER : c->pt == 's' ? PRO_ENT_START : PRO_ENT_END;
            n = 2; tipo = PRO_TK_DIM_LINE_POINT;
        } else if (c->tipo == 4) {
            ents[0] = sk_proj[c->ref]; pts[0] = PRO_ENT_WHOLE;
            ents[1] = sk_ids[c->ent]; pts[1] = PRO_ENT_WHOLE;
            n = 2; tipo = PRO_TK_DIM_LINE_LINE;
        } else if (c->tipo == 6) {
            ents[0] = sk_ids[c->ent2]; pts[0] = PRO_ENT_WHOLE;
            ents[1] = sk_ids[c->ent];
            pts[1] = c->pt == 'w' ? PRO_ENT_WHOLE : c->pt == 'c' ? PRO_ENT_CENTER : c->pt == 's' ? PRO_ENT_START : PRO_ENT_END;
            n = 2; tipo = c->pt == 'w' ? PRO_TK_DIM_LINE_LINE : PRO_TK_DIM_LINE_POINT;
        } else {
            ents[0] = sk_ids[c->ent]; pts[0] = PRO_ENT_WHOLE;
            ents[1] = sk_ids[c->ent2]; pts[1] = PRO_ENT_WHOLE;
            n = 2; tipo = PRO_TK_DIM_LINE_LINE;
        }
        if (ProSecdimCreate(sec, ents, pts, n, tipo, lugar, &did) != PRO_TK_NO_ERROR ||
            (c->tipo == 6 && ProSecdimDiameterSet(sec, did) != PRO_TK_NO_ERROR) ||
            ProSecdimValueSet(sec, did, c->valor) != PRO_TK_NO_ERROR) {
            if (did >= 0) ProSecdimDelete(sec, did);
            cotas_fallidas++;
            continue;
        }
        if (n_own < MAX_COTAS) own[n_own++] = did;
    }
    ProSecerrorAlloc(&err);
    *fase = "acotar";
    e = ProSectionAutodim(sec, &err);
    if (e == PRO_TK_NO_ERROR) { *fase = "regenerar"; e = ProSectionRegenerate(sec, &err); }
    if (e != PRO_TK_NO_ERROR && n_own > 0) {
        /* Las cotas del boceto chocan con alguna restriccion que pone Creo: se quitan todas y
           se acota solo con el acotado automatico (comportamiento de 0.7.x). */
        ProIntlist ids = NULL;
        int nd = 0, k;
        errores_seccion_json(&err);
        if (ProSecdimIdsGet(sec, &ids, &nd) == PRO_TK_NO_ERROR && ids) {
            for (k = 0; k < nd; k++) ProSecdimDelete(sec, ids[k]);
            ProArrayFree((ProArray *)&ids);
        }
        ProSecerrorFree(&err);
        ProSecerrorAlloc(&err);
        n_own = 0;
        cotas_estado = "automaticas_por_conflicto";
        *fase = "acotar";
        e = ProSectionAutodim(sec, &err);
        if (e == PRO_TK_NO_ERROR) { *fase = "regenerar"; e = ProSectionRegenerate(sec, &err); }
    }
    if (e != PRO_TK_NO_ERROR) errores_seccion_json(&err);
    ProSecerrorFree(&err);
    cotas_propias = n_own;
    if (e == PRO_TK_NO_ERROR) {
        /* Cotas finales del croquis y desviacion de la geometria resuelta frente a la pedida */
        ProIntlist ids = NULL;
        int nd = 0, k, j;
        n_cotas_res = 0;
        if (ProSecdimIdsGet(sec, &ids, &nd) == PRO_TK_NO_ERROR && ids) {
            for (k = 0; k < nd && n_cotas_res < MAX_COTAS; k++) {
                ProSecdimType t = PRO_TK_DIM_NONE;
                double v = 0;
                ProSecdimTypeGet(sec, ids[k], &t);
                ProSecdimValueGet(sec, ids[k], &v);
                cotas_res[n_cotas_res].tipo = (int)t;
                cotas_res[n_cotas_res].valor = v;
                cotas_res[n_cotas_res].propia = 0;
                for (j = 0; j < n_own; j++) if (own[j] == ids[k]) cotas_res[n_cotas_res].propia = 1;
                n_cotas_res++;
            }
            ProArrayFree((ProArray *)&ids);
        }
        desviacion_sk = 0;
        for (i = 0; i < n_sk; i++) {
            EntSk *x = &sk_ent[i];
            Pro2dEntdef *d = NULL;
            double p1[2], p2[2], dv = 0;
            if (ProSectionEntityGet(sec, sk_ids[i], &d) != PRO_TK_NO_ERROR || d == NULL) continue;
            if (x->tipo == 1 && d->type == PRO_2D_LINE) {
                Pro2dLinedef *l = (Pro2dLinedef *)d;
                double da, db;
                a_seccion(m, x->v, p1); a_seccion(m, x->v + 3, p2);
                da = fmax(hypot(l->end1[0] - p1[0], l->end1[1] - p1[1]), hypot(l->end2[0] - p2[0], l->end2[1] - p2[1]));
                db = fmax(hypot(l->end1[0] - p2[0], l->end1[1] - p2[1]), hypot(l->end2[0] - p1[0], l->end2[1] - p1[1]));
                dv = da < db ? da : db;
            } else if (x->tipo == 2 && d->type == PRO_2D_ARC) {
                Pro2dArcdef *a = (Pro2dArcdef *)d;
                a_seccion(m, x->v, p1); a_seccion(m, x->v + 3, p2);
                dv = fmax(hypot(a->center[0] - p1[0], a->center[1] - p1[1]),
                          fabs(a->radius - hypot(p2[0] - p1[0], p2[1] - p1[1])));
            } else if (x->tipo == 3 && d->type == PRO_2D_CIRCLE) {
                Pro2dCircledef *ci = (Pro2dCircledef *)d;
                a_seccion(m, x->v, p1);
                dv = fmax(hypot(ci->center[0] - p1[0], ci->center[1] - p1[1]), fabs(ci->radius - x->v[3]));
            }
            if (dv > desviacion_sk) desviacion_sk = dv;
        }
    }
    return e;
}

/* Fija un entero de primer nivel del arbol extraido (lo crea si falta). */
static ProError fijar_entero_arbol(ProElement arbol, int elem_id, int valor)
{
    ProElempath path = NULL;
    ProElempathItem it[1];
    ProElement el = NULL;
    ProError e;
    it[0].type = PRO_ELEM_PATH_ITEM_TYPE_ID; it[0].path_item.elem_id = elem_id;
    ProElempathAlloc(&path);
    ProElempathDataSet(path, it, 1);
    e = ProElemtreeElementGet(arbol, path, &el);
    ProElempathFree(&path);
    if (e != PRO_TK_NO_ERROR || el == NULL) {
        if ((e = ProElementAlloc((ProElemId)elem_id, &el)) != PRO_TK_NO_ERROR) return e;
        if ((e = ProElementIntegerSet(el, valor)) != PRO_TK_NO_ERROR) return e;
        return ProElemtreeElementAdd(arbol, NULL, el);
    }
    return ProElementIntegerSet(el, valor);
}

/* Extension de las caras creadas por la operacion a lo largo de un eje del modelo. */
typedef struct { int eje; double lo, hi; int n; } RangoOp;
static ProError visita_cara_rango(ProGeomitem *it, ProError st, ProAppData d)
{
    RangoOp *r = (RangoOp *)d;
    ProSurface s = NULL;
    ProGeomitemdata *gd = NULL;
    if (ProGeomitemToSurface(it, &s) != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
    if (ProSurfaceDataGet(s, &gd) == PRO_TK_NO_ERROR && gd != NULL && gd->data.p_surface_data != NULL) {
        ProSurfacedata *sd = gd->data.p_surface_data;
        double a = sd->xyz_min[r->eje], b = sd->xyz_max[r->eje];
        if (a > b) { double t = a; a = b; b = t; }
        if (r->n == 0 || a < r->lo) r->lo = a;
        if (r->n == 0 || b > r->hi) r->hi = b;
        r->n++;
        ProGeomitemdataFree(&gd);
    }
    return PRO_TK_NO_ERROR;
}

/* sentido: 0 = el de Creo; -1/1 = PRO_E_STD_DIRECTION. probar: reintenta con el contrario.
   lado: 0 = el de Creo; 1/2 = PRO_E_STD_MATRLSIDE.
   comprobacion: 0 ninguna, 1 volumen exacto (vol_esperado, con signo), 2 solo signo (vol_esperado = -1/1),
   3 como mucho |vol_esperado| con su signo (exacto si la pieza estaba vacia).
   eje_r >= 0: las caras de la operacion deben quedar entre r_min y r_max en ese eje (comprueba el sentido). */
static int crear_extrusion(ProMdl mdl, ProElement raiz, int sentido, int probar, int lado,
                           int comprobacion, double vol_esperado, int eje_r, double r_min, double r_max)
{
    RangoOp rg;
    ProModelitem mi;
    ProSelection msel = NULL;
    ProAsmcomppath cp;
    int intento, max_intentos = (probar && sentido != 0) ? 2 : 1;
    double ultimo_delta = 0, v0 = -1, v1 = -1;
    ProError ultimo = PRO_TK_GENERAL_ERROR;
    const char *fase = "crear";

    memset(&rg, 0, sizeof(rg));
    sk_err_txt[0] = '\0';
    ProMdlToModelitem(mdl, &mi);
    ProSelectionAlloc(NULL, &mi, &msel);
    ProSelectionAsmcomppathGet(msel, &cp);
    for (intento = 0; intento < max_intentos; intento++) {
        ProFeature feat;
        ProErrorlist errs;
        ProFeatureCreateOptions *opts = NULL;
        ProElement arbol = NULL, sk = NULL;
        ProElempath path = NULL;
        ProElempathItem items[2];
        ProSection sec = NULL;
        ProFeatStatus st = PRO_FEAT_ACTIVE;
        int s_act = intento ? -sentido : sentido;
        sk_err_txt[0] = '\0';
        n_cotas_res = 0; cotas_propias = 0; cotas_fallidas = 0; desviacion_sk = -1;
        v0 = volumen_pieza(mdl);
        memset(&feat, 0, sizeof(feat));
        ProArrayAlloc(1, sizeof(ProFeatureCreateOptions), 1, (ProArray *)&opts);
        opts[0] = PRO_FEAT_CR_INCOMPLETE_FEAT;
        fase = "crear";
        ultimo = ProFeatureWithoptionsCreate(msel, raiz, opts, PRO_REGEN_NO_FLAGS, &feat, &errs);
        ProArrayFree((ProArray *)&opts);
        if (ultimo != PRO_TK_NO_ERROR) { if (feat.id > 0) borrar_ids((ProSolid)mdl, &feat.id, 1); break; }
        fase = "arbol";
        ultimo = ProFeatureElemtreeExtract(&feat, NULL, PRO_FEAT_EXTRACT_NO_OPTS, &arbol);
        if (ultimo == PRO_TK_NO_ERROR) {
            items[0].type = PRO_ELEM_PATH_ITEM_TYPE_ID; items[0].path_item.elem_id = PRO_E_STD_SECTION;
            items[1].type = PRO_ELEM_PATH_ITEM_TYPE_ID; items[1].path_item.elem_id = PRO_E_SKETCHER;
            ProElempathAlloc(&path);
            ProElempathDataSet(path, items, 2);
            fase = "croquis";
            ultimo = ProElemtreeElementGet(arbol, path, &sk);
            if (ultimo == PRO_TK_NO_ERROR) ultimo = ProElementSpecialvalueGet(sk, NULL, (ProAppData *)&sec);
            ProElempathFree(&path);
        }
        if (ultimo == PRO_TK_NO_ERROR) ultimo = construir_seccion(mdl, sec, &fase);
        /* sentido y lado del material: solo cuentan con la seccion ya definida (ProExtrude.h, nota 3) */
        if (ultimo == PRO_TK_NO_ERROR && lado) { fase = "lado_material"; ultimo = fijar_entero_arbol(arbol, PRO_E_STD_MATRLSIDE, lado); }
        if (ultimo == PRO_TK_NO_ERROR && s_act) { fase = "sentido"; ultimo = fijar_entero_arbol(arbol, PRO_E_STD_DIRECTION, s_act); }
        if (ultimo == PRO_TK_NO_ERROR) {
            ProArrayAlloc(1, sizeof(ProFeatureCreateOptions), 1, (ProArray *)&opts);
            opts[0] = PRO_FEAT_CR_DEFINE_MISS_ELEMS;
            fase = "completar";
            ultimo = ProFeatureWithoptionsRedefine(&cp, &feat, arbol, opts, PRO_REGEN_NO_FLAGS, &errs);
            ProArrayFree((ProArray *)&opts);
        }
        if (arbol) ProFeatureElemtreeFree(&feat, arbol);
        if (ultimo == PRO_TK_NO_ERROR) { ProFeatureStatusGet(&feat, &st); if (st != PRO_FEAT_ACTIVE) { ultimo = PRO_TK_GENERAL_ERROR; fase = "completar"; } }
        if (ultimo != PRO_TK_NO_ERROR) {
            int fid = feat.id;
            if (fid > 0) borrar_ids((ProSolid)mdl, &fid, 1);
            /* un error del croquis no se arregla cambiando el sentido */
            if (strcmp(fase, "completar") != 0) break;
            continue;
        }
        v1 = volumen_pieza(mdl);
        ultimo_delta = (v0 >= 0 && v1 >= 0) ? v1 - v0 : 0;
        memset(&rg, 0, sizeof(rg));
        if (eje_r >= 0 && eje_r <= 2) {
            double tol = 2e-3;
            rg.eje = eje_r;
            ProFeatureGeomitemVisit(&feat, PRO_SURFACE, visita_cara_rango, NULL, (ProAppData)&rg);
            if (rg.n == 0 || rg.lo < r_min - tol || rg.hi > r_max + tol) {
                int fid = feat.id;
                borrar_ids((ProSolid)mdl, &fid, 1);
                ultimo = PRO_TK_GENERAL_ERROR;
                fase = "sentido_incorrecto";
                continue;
            }
        }
        if (comprobacion && v1 >= 0) {
            int mal;
            if (v0 < 0) { v0 = 0; ultimo_delta = v1; }   /* primera operacion de una pieza vacia */
            if (comprobacion == 1 || (comprobacion == 3 && v0 <= 1e-9))
                mal = fabs(ultimo_delta - vol_esperado) > 0.02 * fabs(vol_esperado) + 0.01;
            else if (comprobacion == 3)
                mal = (vol_esperado > 0 ? ultimo_delta <= 1e-6 : ultimo_delta >= -1e-6) ||
                      fabs(ultimo_delta) > 1.02 * fabs(vol_esperado) + 0.01;
            else mal = (vol_esperado > 0) ? ultimo_delta <= 1e-6 : ultimo_delta >= -1e-6;
            if (mal) {
                int fid = feat.id;
                borrar_ids((ProSolid)mdl, &fid, 1);
                ultimo = PRO_TK_GENERAL_ERROR;
                fase = "verificacion";
                continue;
            }
        }
        {
            ProName nombre;
            nombre[0] = L'\0';
            ProModelitemNameGet((ProModelitem *)&feat, nombre);
            cabecera_ok();
            modelo_json("modelo", mdl);
            j_clave_obj("operacion");
            j_int("id", feat.id);
            j_wstr("nombre", nombre);
            j_int("intentos", intento + 1);
            j_int("sentido", s_act);
            j_num("volumen_antes", v0);
            j_num("volumen_despues", v1);
            j_num("cambio_volumen", ultimo_delta);
            if (rg.n > 0) { j_num("extension_min", rg.lo); j_num("extension_max", rg.hi); }
            j_str("cotas_croquis", cotas_estado);
            j_int("cotas_boceto_creadas", cotas_propias);
            j_int("cotas_boceto_fallidas", cotas_fallidas);
            if (desviacion_sk >= 0) j_num("desviacion_croquis_mm", desviacion_sk);
            if (sk_err_txt[0]) j_str("avisos_croquis", sk_err_txt);
            j_abrir_lista("cotas");
            {
                int k;
                for (k = 0; k < n_cotas_res; k++) {
                    const char *tn;
                    switch (cotas_res[k].tipo) {
                    case 1: tn = "longitud"; break;
                    case 2: tn = "punto_recta"; break;
                    case 3: tn = "radio"; break;
                    case 4: tn = "diametro"; break;
                    case 5: tn = "entre_rectas"; break;
                    case 6: tn = "entre_puntos"; break;
                    case 7: tn = "entre_puntos_h"; break;
                    case 8: tn = "entre_puntos_v"; break;
                    case 11: tn = "angulo_arco"; break;
                    case 12: tn = "angulo_rectas"; break;
                    case 13: tn = "recta_arco"; break;
                    default: tn = "otra"; break;
                    }
                    j_abrir_obj();
                    j_str("tipo", tn);
                    j_num("valor", cotas_res[k].valor);
                    j_bool("del_boceto", cotas_res[k].propia);
                    j_cerrar_obj();
                }
            }
            j_cerrar_lista();
            j_cerrar_obj();
            j_bool("guardado", 0);
            j_cerrar_obj();
            j_fin();
        }
        ProSelectionFree(&msel); ProElementFree(&raiz);
        return 0;
    }
    ProSelectionFree(&msel); ProElementFree(&raiz);
    if (!strcmp(fase, "sentido_incorrecto")) {
        char txt[240];
        sprintf(txt, "La operacion no queda donde se esperaba en el eje %c (%.3f a %.3f frente a %.3f a %.3f)%s: "
                "no se ha creado", "XYZ"[eje_r], rg.lo, rg.hi, r_min, r_max, max_intentos > 1 ? " en ningun sentido" : "");
        error_json("verificacion", 0, txt);
    } else if (!strcmp(fase, "verificacion")) {
        char txt[240];
        if (comprobacion == 1)
            sprintf(txt, "La extrusion no cambia el volumen como se esperaba (%.3f frente a %.3f mm3)%s: no se ha creado",
                    ultimo_delta, vol_esperado, max_intentos > 1 ? " en ningun sentido" : "");
        else
            sprintf(txt, "La operacion no %s material (cambio de volumen %.3f mm3)%s: no se ha creado",
                    vol_esperado > 0 ? "anade" : "quita", ultimo_delta, max_intentos > 1 ? " en ningun sentido" : "");
        error_json("verificacion", 0, txt);
    } else {
        char txt[900];
        sprintf(txt, "Creo no ha podido crear la extrusion (fase: %s)%s%s; no se ha creado nada", fase,
                sk_err_txt[0] ? ". Croquis: " : "", sk_err_txt);
        error_json(fase, (int)ultimo, txt);
    }
    return 5;
}

static int orden_crear(const char *fichero)
{
    FILE *f;
    char linea[512], modelo[PRO_MDLNAME_SIZE] = "", serie[64] = "", tamano[64] = "";
    char alt1[160] = "", alt2[160] = "", ai1[160] = "", ai2[160] = "";
    double objetivo[3] = {0, 0, 0}, direccion[3] = {0, 0, 0};
    int verificar = 0, id_patron = 0;
    double vol_esperado = 0;
    int sentido = 0, probar_sentidos = 0, lado_mat = 0, comprobacion = 0, eje_r = -1;
    double r_min = 0, r_max = 0;
    int sin_aristas[64], n_sin_aristas = 0;
    ProElement raiz = NULL;
    ProMdl mdl;
    int lineas_mal = 0;

    n_nodos = 0;
    n_esperados = 0;
    n_sk = 0;
    n_sk_refs = 0;
    n_cotas = 0;
    f = fopen(fichero ? fichero : "", "r");
    if (!f) { error_json("plan", -1, "No se puede abrir el fichero del plan"); return 2; }
    while (fgets(linea, sizeof(linea), f)) {
        char *t = linea, clave[32], ruta[160], tipo[4];
        quitar_fin(t);
        if (!*t || *t == '#') continue;
        if (sscanf(t, "%31s", clave) != 1) continue;
        if (!strcmp(clave, "modelo")) { sscanf(t, "%*s %179s", modelo); continue; }
        if (!strcmp(clave, "serie")) { sscanf(t, "%*s %63s", serie); continue; }
        if (!strcmp(clave, "tamano")) { sscanf(t, "%*s %63s", tamano); continue; }
        if (!strcmp(clave, "alternar")) { sscanf(t, "%*s %159s %159s", alt1, alt2); continue; }
        if (!strcmp(clave, "alternar_i")) { sscanf(t, "%*s %159s %159s", ai1, ai2); continue; }
        if (!strcmp(clave, "sin_aristas")) {
            const char *q = t + 11;
            int aid, usados = 0;
            while (n_sin_aristas < 64 && sscanf(q, "%d%n", &aid, &usados) == 1) { sin_aristas[n_sin_aristas++] = aid; q += usados; }
            continue;
        }
        if (!strcmp(clave, "patron")) { if (sscanf(t, "%*s %d", &id_patron) != 1) lineas_mal++; continue; }
        if (!strcmp(clave, "sk_ref")) {
            if (n_sk_refs >= 8 || sscanf(t, "%*s %d %d", &sk_refs[n_sk_refs][0], &sk_refs[n_sk_refs][1]) != 2) lineas_mal++;
            else n_sk_refs++;
            continue;
        }
        if (!strcmp(clave, "sk_linea") || !strcmp(clave, "sk_arco") || !strcmp(clave, "sk_circulo") || !strcmp(clave, "sk_eje")) {
            EntSk *x = &sk_ent[n_sk];
            int eje = !strcmp(clave, "sk_eje");
            int nv = (!strcmp(clave, "sk_linea") || eje) ? 6 : !strcmp(clave, "sk_arco") ? 12 : 4, leidos = 0, k, us;
            const char *q = t + strlen(clave);
            if (n_sk >= MAX_SK) { lineas_mal++; continue; }
            for (k = 0; k < nv; k++) { if (sscanf(q, "%lf%n", &x->v[k], &us) != 1) break; q += us; leidos++; }
            if (leidos != nv) { lineas_mal++; continue; }
            x->tipo = eje ? 4 : nv == 6 ? 1 : nv == 12 ? 2 : 3;
            n_sk++;
            continue;
        }
        if (!strcmp(clave, "sk_cota")) {
            CotaSk *c = &sk_cotas[n_cotas];
            char sub[8] = "", pt[4] = "";
            int ok = 0;
            memset(c, 0, sizeof(*c));
            if (n_cotas >= MAX_COTAS || sscanf(t, "%*s %7s", sub) != 1) { lineas_mal++; continue; }
            if (!strcmp(sub, "diam") || !strcmp(sub, "radio")) {
                c->tipo = sub[0] == 'd' ? 1 : 2;
                ok = sscanf(t, "%*s %*s %d %lf %lf %lf %lf", &c->ent, &c->valor, &c->lugar[0], &c->lugar[1], &c->lugar[2]) == 5;
            } else if (!strcmp(sub, "ref")) {
                c->tipo = 3;
                ok = sscanf(t, "%*s %*s %d %3s %d %lf %lf %lf %lf", &c->ent, pt, &c->ref, &c->valor,
                            &c->lugar[0], &c->lugar[1], &c->lugar[2]) == 7 && strchr("cse", pt[0]) && !pt[1];
                c->pt = pt[0];
            } else if (!strcmp(sub, "refl")) {
                c->tipo = 4;
                ok = sscanf(t, "%*s %*s %d %d %lf %lf %lf %lf", &c->ent, &c->ref, &c->valor, &c->lugar[0], &c->lugar[1], &c->lugar[2]) == 6;
            } else if (!strcmp(sub, "diaeje")) {
                c->tipo = 6;
                ok = sscanf(t, "%*s %*s %d %3s %d %lf %lf %lf %lf", &c->ent, pt, &c->ent2, &c->valor,
                            &c->lugar[0], &c->lugar[1], &c->lugar[2]) == 7 && strchr("csew", pt[0]) && !pt[1];
                c->pt = pt[0];
            } else if (!strcmp(sub, "entre")) {
                c->tipo = 5;
                ok = sscanf(t, "%*s %*s %d %d %lf %lf %lf %lf", &c->ent, &c->ent2, &c->valor, &c->lugar[0], &c->lugar[1], &c->lugar[2]) == 6;
            }
            if (!ok || c->valor < 0) lineas_mal++; else n_cotas++;
            continue;
        }
        if (!strcmp(clave, "volumen")) { if (sscanf(t, "%*s %lf", &vol_esperado) == 1) comprobacion = 1; else lineas_mal++; continue; }
        if (!strcmp(clave, "volumen_signo")) {
            int sg;
            if (sscanf(t, "%*s %d", &sg) == 1 && (sg == 1 || sg == -1)) { comprobacion = 2; vol_esperado = sg; } else lineas_mal++;
            continue;
        }
        if (!strcmp(clave, "sentido")) {
            if (sscanf(t, "%*s %d", &sentido) != 1 || (sentido != 1 && sentido != -1 && sentido != 0)) lineas_mal++;
            continue;
        }
        if (!strcmp(clave, "probar_sentidos")) { probar_sentidos = 1; continue; }
        if (!strcmp(clave, "volumen_max")) { if (sscanf(t, "%*s %lf", &vol_esperado) == 1 && vol_esperado != 0) comprobacion = 3; else lineas_mal++; continue; }
        if (!strcmp(clave, "rango")) {
            char ej[4];
            if (sscanf(t, "%*s %3s %lf %lf", ej, &r_min, &r_max) != 3 || !ej[0] || !strchr("XYZ", ej[0]) || ej[1] || r_min >= r_max) lineas_mal++;
            else eje_r = ej[0] - 'X';
            continue;
        }
        if (!strcmp(clave, "lado_material")) {
            if (sscanf(t, "%*s %d", &lado_mat) != 1 || lado_mat < 0 || lado_mat > 3) lineas_mal++;
            continue;
        }
        if (!strcmp(clave, "esperado")) {
            if (n_esperados >= MAX_ESPERADOS ||
                sscanf(t, "%*s %lf %lf %lf", &esperados[n_esperados][0], &esperados[n_esperados][1],
                       &esperados[n_esperados][2]) != 3) { lineas_mal++; continue; }
            n_esperados++;
            continue;
        }
        if (!strcmp(clave, "verificar")) {
            verificar = sscanf(t, "%*s %lf %lf %lf %lf %lf %lf", &objetivo[0], &objetivo[1], &objetivo[2],
                               &direccion[0], &direccion[1], &direccion[2]) == 6;
            continue;
        }
        if (!strcmp(clave, "e")) {
            ProElement el;
            if (sscanf(t, "%*s %159s %3s", ruta, tipo) != 2) { lineas_mal++; continue; }
            if (!modelo[0]) { lineas_mal++; continue; }
            if (!(el = nodo_ruta(ruta, &raiz))) { lineas_mal++; continue; }
            if (tipo[0] == 'i') {
                int v; if (sscanf(t, "%*s %*s %*s %d", &v) == 1) ProElementIntegerSet(el, v); else lineas_mal++;
            } else if (tipo[0] == 'd') {
                double v; if (sscanf(t, "%*s %*s %*s %lf", &v) == 1) { ProElementDecimalsSet(el, 4); ProElementDoubleSet(el, v); } else lineas_mal++;
            } else if (tipo[0] == 'b') {
                int v; if (sscanf(t, "%*s %*s %*s %d", &v) == 1) ProElementBooleanSet(el, v ? PRO_B_TRUE : PRO_B_FALSE); else lineas_mal++;
            } else if (tipo[0] == 'w') {
                const char *txt = strstr(t, " w ");
                wchar_t wv[256];
                if (txt) { ProStringToWstring(wv, (char *)(txt + 3)); ProElementWstringSet(el, wv); } else lineas_mal++;
            } else if (tipo[0] == 'r') {
                int rt, rid;
                ProModelitem it;
                ProSelection sel = NULL;
                ProReference ref = NULL;
                if (sscanf(t, "%*s %*s %*s %d %d", &rt, &rid) != 2) { lineas_mal++; continue; }
                if (!modelo_objetivo(modelo, &mdl)) { fclose(f); return 4; }
                if (ProModelitemInit(mdl, rid, (ProType)rt, &it) != PRO_TK_NO_ERROR ||
                    ProSelectionAlloc(NULL, &it, &sel) != PRO_TK_NO_ERROR ||
                    ProSelectionToReference(sel, &ref) != PRO_TK_NO_ERROR ||
                    ProElementReferenceSet(el, ref) != PRO_TK_NO_ERROR) {
                    fclose(f);
                    error_json("plan", rid, "Referencia no valida (el elemento no existe en la pieza)");
                    return 2;
                }
                ProSelectionFree(&sel);
            } else if (tipo[0] == 'k') {
                /* coleccion de aristas: e <ruta> k id1 id2 ... (redondeos) */
                ProCollection col = NULL;
                const char *q = strstr(t, " k ");
                int n_ar = 0;
                if (!q) { lineas_mal++; continue; }
                if (!modelo_objetivo(modelo, &mdl)) { fclose(f); return 4; }
                if (ProCrvcollectionAlloc(&col) != PRO_TK_NO_ERROR) { lineas_mal++; continue; }
                q += 3;
                while (*q) {
                    int aid, usados = 0;
                    ProModelitem it;
                    ProSelection sel = NULL;
                    ProReference ref = NULL;
                    ProCrvcollinstr ins = NULL;
                    if (sscanf(q, "%d%n", &aid, &usados) != 1) break;
                    q += usados;
                    while (*q == ' ') q++;
                    if (ProModelitemInit(mdl, aid, PRO_EDGE, &it) != PRO_TK_NO_ERROR ||
                        ProSelectionAlloc(NULL, &it, &sel) != PRO_TK_NO_ERROR ||
                        ProSelectionToReference(sel, &ref) != PRO_TK_NO_ERROR ||
                        ProCrvcollinstrAlloc(PRO_CURVCOLL_ADD_ONE_INSTR, &ins) != PRO_TK_NO_ERROR ||
                        ProCrvcollinstrReferenceAdd(ins, ref) != PRO_TK_NO_ERROR ||
                        ProCrvcollectionInstructionAdd(col, ins) != PRO_TK_NO_ERROR) {
                        fclose(f);
                        error_json("plan", aid, "Arista no valida (no existe en la pieza)");
                        return 2;
                    }
                    ProSelectionFree(&sel);
                    n_ar++;
                }
                if (n_ar == 0 || ProElementCollectionSet(el, col) != PRO_TK_NO_ERROR) lineas_mal++;
            } /* 'c': compuesto, ya creado */
            continue;
        }
        lineas_mal++;
    }
    fclose(f);
    if (lineas_mal || raiz == NULL || !modelo[0]) {
        error_json("plan", lineas_mal, "Plan incompleto o con lineas no validas");
        return 2;
    }
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    if (id_patron > 0) return crear_patron(mdl, raiz, id_patron, direccion, ai1, ai2);
    if (n_sk > 0) return crear_extrusion(mdl, raiz, sentido, probar_sentidos, lado_mat, comprobacion, vol_esperado, eje_r, r_min, r_max);
    if (serie[0]) {
        wchar_t w[64];
        ProStringToWstring(w, serie);
        if (ProElementHoleThreadSeriesSet(raiz, mdl, w) != PRO_TK_NO_ERROR) {
            error_json("plan", 0, "Serie de rosca no encontrada en las tablas de Creo (.hol)"); return 2;
        }
    }
    if (tamano[0]) {
        wchar_t w[64];
        ProStringToWstring(w, tamano);
        if (ProElementHoleScrewSizeSet(raiz, mdl, w) != PRO_TK_NO_ERROR) {
            error_json("plan", 0, "Tamano de tornillo no encontrado en la serie de rosca"); return 2;
        }
    }

    {
        ProModelitem mi;
        ProSelection msel = NULL;
        ProFeatureCreateOptions *opts = NULL;
        ProElement e1 = alt1[0] ? buscar_nodo(alt1) : NULL, e2 = alt2[0] ? buscar_nodo(alt2) : NULL;
        double v1 = 0, v2 = 0;
        static const int signos[4][2] = {{1, 1}, {-1, 1}, {1, -1}, {-1, -1}};
        int intento, max_intentos = (verificar && (e1 || e2)) ? 4 : 1;
        ProError ultimo = PRO_TK_GENERAL_ERROR;
        double ultima_dist = -1, ultimo_cos = 0;

        if (e1) ProElementDoubleGet(e1, NULL, &v1);
        if (e2) ProElementDoubleGet(e2, NULL, &v2);
        ProMdlToModelitem(mdl, &mi);
        ProSelectionAlloc(NULL, &mi, &msel);
        ProArrayAlloc(1, sizeof(ProFeatureCreateOptions), 1, (ProArray *)&opts);
        opts[0] = PRO_FEAT_CR_NO_OPTS;

        for (intento = 0; intento < max_intentos; intento++) {
            ProFeature feat;
            ProErrorlist errs;
            ProFeatStatus st = PRO_FEAT_ACTIVE;
            EjeLeido eje;
            if (e1) ProElementDoubleSet(e1, v1 * signos[intento][0]);
            if (e2) ProElementDoubleSet(e2, v2 * signos[intento][1]);
            memset(&feat, 0, sizeof(feat));
            ultimo = ProFeatureWithoptionsCreate(msel, raiz, opts, PRO_REGEN_NO_RESOLVE_MODE, &feat, &errs);
            if (ultimo != PRO_TK_NO_ERROR) {
                if (feat.id > 0) borrar_ids((ProSolid)mdl, &feat.id, 1);
                continue;
            }
            ProFeatureStatusGet(&feat, &st);
            if (st != PRO_FEAT_ACTIVE) {
                borrar_ids((ProSolid)mdl, &feat.id, 1);
                ultimo = PRO_TK_GENERAL_ERROR;
                continue;
            }
            if (n_sin_aristas > 0) {
                /* redondeo/chaflan: las aristas pedidas deben haber desaparecido */
                int k, quedan = -1;
                for (k = 0; k < n_sin_aristas; k++) {
                    ProEdge ed;
                    if (ProEdgeInit((ProSolid)mdl, sin_aristas[k], &ed) == PRO_TK_NO_ERROR) { quedan = sin_aristas[k]; break; }
                }
                if (quedan >= 0) {
                    borrar_ids((ProSolid)mdl, &feat.id, 1);
                    error_json("verificacion", quedan, "La arista sigue en la pieza tras crear la operacion: se ha borrado");
                    ProArrayFree((ProArray *)&opts); ProSelectionFree(&msel); ProElementFree(&raiz);
                    return 6;
                }
            }
            memset(&eje, 0, sizeof(eje));
            ProFeatureGeomitemVisit(&feat, PRO_AXIS, visita_eje_creado, NULL, &eje);
            if (verificar) {
                if (!eje.hay) {
                    borrar_ids((ProSolid)mdl, &feat.id, 1);
                    error_json("verificacion", feat.id, "La operacion creada no tiene eje: se ha borrado");
                    ProArrayFree((ProArray *)&opts); ProSelectionFree(&msel); ProElementFree(&raiz);
                    return 6;
                }
                comprobar_eje(eje.a, eje.b, objetivo, direccion, &ultima_dist, &ultimo_cos);
                if (ultima_dist > 0.005 || ultimo_cos < 0.9999) {
                    borrar_ids((ProSolid)mdl, &feat.id, 1);
                    ultimo = PRO_TK_GENERAL_ERROR;
                    continue;
                }
            }
            /* correcto */
            {
                ProName nombre;
                nombre[0] = L'\0';
                ProModelitemNameGet((ProModelitem *)&feat, nombre);
                cabecera_ok();
                modelo_json("modelo", mdl);
                j_clave_obj("operacion");
                j_int("id", feat.id);
                j_wstr("nombre", nombre);
                j_int("intentos", intento + 1);
                j_abrir_lista("signos"); j_int(NULL, signos[intento][0]); j_int(NULL, signos[intento][1]); j_cerrar_lista();
                if (eje.hay) {
                    j_abrir_lista("eje");
                    j_num(NULL, eje.a[0]); j_num(NULL, eje.a[1]); j_num(NULL, eje.a[2]);
                    j_num(NULL, eje.b[0]); j_num(NULL, eje.b[1]); j_num(NULL, eje.b[2]);
                    j_cerrar_lista();
                }
                if (verificar) { j_num("desviacion", ultima_dist); j_num("coseno_direccion", ultimo_cos); }
                j_cerrar_obj();
                j_bool("guardado", 0);
                j_cerrar_obj();
                j_fin();
            }
            ProArrayFree((ProArray *)&opts); ProSelectionFree(&msel); ProElementFree(&raiz);
            return 0;
        }
        ProArrayFree((ProArray *)&opts); ProSelectionFree(&msel); ProElementFree(&raiz);
        if (verificar && ultima_dist >= 0)
            error_json("verificacion", (int)(ultima_dist * 1000),
                       "La operacion no quedaba donde se pedia con ninguna combinacion de signos: no se ha creado");
        else
            error_json("creacion", (int)ultimo, "Creo no ha podido crear la operacion (revisa el plan); no se ha creado nada");
        return 5;
    }
}

/* ----------------------------------------------------------------- borrar */

/* La operacion es miembro o cabecera de un patron cuya cabecera esta en la lista. */
static int cabecera_en_lista(ProMdl mdl, int id, const int *ids, int n)
{
    ProFeature fe, cab;
    ProPattern pat;
    ProPatternStatus ps = PRO_PATTERN_NONE;
    int j;
    if (ProModelitemInit(mdl, id, PRO_FEATURE, (ProModelitem *)&fe) != PRO_TK_NO_ERROR) return 0;
    ProFeaturePatternStatusGet(&fe, &ps);
    if (ps != PRO_PATTERN_MEMBER && ps != PRO_PATTERN_HEADER) return 0;
    if (ProFeaturePatternGet(&fe, PRO_FEAT_PATTERN, &pat) != PRO_TK_NO_ERROR) return 0;
    if (ProPatternHeaderGet(&pat, &cab) != PRO_TK_NO_ERROR) return 0;
    for (j = 0; j < n; j++) if (ids[j] == cab.id) return 1;
    return 0;
}

static int orden_borrar(const char *modelo, const char *ids_txt)
{
    ProMdl mdl;
    int ids[64], n = 0, i;
    char copia[512], *tok;
    if (!ids_txt || !*ids_txt) { error_json("argumentos", -1, "Faltan los ids (--ids 12,15)"); return 2; }
    if (!modelo_objetivo(modelo, &mdl)) return 4;
    strncpy(copia, ids_txt, sizeof(copia) - 1); copia[sizeof(copia) - 1] = '\0';
    for (tok = strtok(copia, ","); tok && n < 64; tok = strtok(NULL, ",")) {
        ProFeature fe;
        ProName nm;
        char nombre[64];
        int id = atoi(tok);
        ProFeatStatus est;
        nm[0] = L'\0';
        if (ProModelitemInit(mdl, id, PRO_FEATURE, (ProModelitem *)&fe) != PRO_TK_NO_ERROR ||
            ProFeatureStatusGet(&fe, &est) != PRO_TK_NO_ERROR) {
            error_json("borrar", id, "No existe la operacion"); return 4;
        }
        if (ProModelitemNameGet((ProModelitem *)&fe, nm) != PRO_TK_NO_ERROR) nm[0] = L'\0';  /* sin nombre */
        ProWstringToString(nombre, nm);
        if (_strnicmp(nombre, "IA_", 3) != 0) {
            error_json("borrar", id, "Solo se borran operaciones creadas por el asistente (nombre IA_*)"); return 4;
        }
        ids[n++] = id;
    }
    /* Con PRO_FEAT_DELETE_CLIP se borrarian tambien las hijas: si alguna no esta en la
       lista (p. ej. una operacion que el usuario hizo despues sobre el taladro), no se borra nada. */
    for (i = 0; i < n; i++) {
        ProFeature fe;
        int *hijas = NULL, nh = 0, k, j;
        ProModelitemInit(mdl, ids[i], PRO_FEATURE, (ProModelitem *)&fe);
        if (ProFeatureChildrenGet(&fe, &hijas, &nh) != PRO_TK_NO_ERROR) continue;
        for (k = 0; k < nh; k++) {
            int esta = 0;
            for (j = 0; j < n; j++) if (ids[j] == hijas[k]) esta = 1;
            if (!esta && cabecera_en_lista(mdl, hijas[k], ids, n)) esta = 1;
            if (!esta) {
                int h = hijas[k];
                ProArrayFree((ProArray *)&hijas);
                error_json("borrar", h, "La operacion tiene operaciones hijas que no son del asistente: no se borra nada (borrala a mano)");
                return 4;
            }
        }
        ProArrayFree((ProArray *)&hijas);
    }
    /* primero los patrones IA_* (ProPatternDelete deja la guia), luego el resto */
    {
        int resto[64], nr = 0;
        for (i = 0; i < n; i++) {
            ProFeature fe;
            ProFeattype t = 0;
            ProPattern pat;
            ProModelitemInit(mdl, ids[i], PRO_FEATURE, (ProModelitem *)&fe);
            ProFeatureTypeGet(&fe, &t);
            if (t == PRO_FEAT_PATTERN_HEAD) {
                if (ProFeaturePatternGet(&fe, PRO_FEAT_PATTERN, &pat) != PRO_TK_NO_ERROR ||
                    ProPatternDelete(&pat) != PRO_TK_NO_ERROR) {
                    error_json("borrar", ids[i], "Creo no ha podido borrar el patron"); return 5;
                }
            } else {
                resto[nr++] = ids[i];
            }
        }
        if (nr > 0 && borrar_ids((ProSolid)mdl, resto, nr) != PRO_TK_NO_ERROR) {
            error_json("borrar", 0, "Creo no ha podido borrar las operaciones"); return 5;
        }
    }
    cabecera_ok();
    modelo_json("modelo", mdl);
    j_abrir_lista("borradas");
    for (i = 0; i < n; i++) j_int(NULL, ids[i]);
    j_cerrar_lista();
    j_bool("guardado", 0);
    j_cerrar_obj();
    j_fin();
    return 0;
}

/* ------------------------------------------------------------------ main */

int main(int argc, char *argv[])
{
    const char *modelo = NULL, *plan = NULL, *ids = NULL, *pares = NULL;
    int rapido = 0, i, rc;
    static const char *ordenes[] = {"estado", "interferencias", "taladros", "posiciones", "caras",
                                    "seleccion", "crear", "borrar", "arbol", "aristas", "holguras", NULL};
    int valida = 0;

    orden_actual = argc > 1 ? argv[1] : "";
    for (i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--modelo") == 0 && i + 1 < argc) modelo = argv[++i];
        else if (strcmp(argv[i], "--plan") == 0 && i + 1 < argc) plan = argv[++i];
        else if (strcmp(argv[i], "--ids") == 0 && i + 1 < argc) ids = argv[++i];
        else if (strcmp(argv[i], "--pares") == 0 && i + 1 < argc) pares = argv[++i];
        else if (strcmp(argv[i], "--rapido") == 0) rapido = 1;
    }
    for (i = 0; ordenes[i]; i++) if (!strcmp(orden_actual, ordenes[i])) valida = 1;
    if (!valida) {
        error_json("argumentos", -1, "Orden desconocida. Ordenes: estado, interferencias, taladros, posiciones, "
                   "caras, seleccion, crear, borrar, arbol, aristas, holguras");
        return 2;
    }
    if (!conectar()) return 3;

    if (!strcmp(orden_actual, "estado")) rc = orden_estado();
    else if (!strcmp(orden_actual, "interferencias")) rc = orden_interferencias(modelo, rapido);
    else if (!strcmp(orden_actual, "taladros")) rc = orden_taladros(modelo);
    else if (!strcmp(orden_actual, "posiciones")) rc = orden_posiciones(modelo);
    else if (!strcmp(orden_actual, "caras")) rc = orden_caras(modelo);
    else if (!strcmp(orden_actual, "seleccion")) rc = orden_seleccion();
    else if (!strcmp(orden_actual, "crear")) rc = orden_crear(plan);
    else if (!strcmp(orden_actual, "arbol")) rc = orden_arbol(modelo, ids);
    else if (!strcmp(orden_actual, "aristas")) rc = orden_aristas(modelo);
    else if (!strcmp(orden_actual, "holguras")) rc = orden_holguras(modelo, pares);
    else rc = orden_borrar(modelo, ids);

    ProEngineerDisconnect(&sesion, 10);
    return rc;
}
