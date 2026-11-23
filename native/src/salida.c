/* creo_iac_tk - salida JSON minima. Sin dependencias de Toolkit. */
#include <stdio.h>
#include <string.h>
#include "salida.h"

static int primero[64];
static int nivel = 0;

static void sep(void)
{
    if (nivel > 0) {
        if (!primero[nivel]) fputc(',', stdout);
        primero[nivel] = 0;
    }
}

static void escribir_cadena(const char *s)
{
    fputc('"', stdout);
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        switch (c) {
        case '"':  fputs("\\\"", stdout); break;
        case '\\': fputs("\\\\", stdout); break;
        case '\n': fputs("\\n", stdout); break;
        case '\r': fputs("\\r", stdout); break;
        case '\t': fputs("\\t", stdout); break;
        default:
            if (c < 0x20) fprintf(stdout, "\\u%04x", c);
            else fputc(c, stdout);
        }
    }
    fputc('"', stdout);
}

static void clave(const char *k)
{
    sep();
    if (k) { escribir_cadena(k); fputc(':', stdout); }
}

void j_abrir_obj(void)            { clave(NULL); fputc('{', stdout); primero[++nivel] = 1; }
void j_clave_obj(const char *k)   { clave(k); fputc('{', stdout); primero[++nivel] = 1; }
void j_cerrar_obj(void)           { fputc('}', stdout); nivel--; }
void j_abrir_lista(const char *k) { clave(k); fputc('[', stdout); primero[++nivel] = 1; }
void j_cerrar_lista(void)         { fputc(']', stdout); nivel--; }
void j_str(const char *k, const char *v) { clave(k); if (v) escribir_cadena(v); else fputs("null", stdout); }
void j_wstr(const char *k, const wchar_t *v)
{
    char buf[4096];
    if (!v) { j_null(k); return; }
    a_utf8(v, buf, sizeof(buf));
    j_str(k, buf);
}
void j_int(const char *k, long v)   { clave(k); fprintf(stdout, "%ld", v); }
void j_num(const char *k, double v)  { clave(k); fprintf(stdout, "%.9g", v); }
void j_bool(const char *k, int v)    { clave(k); fputs(v ? "true" : "false", stdout); }
void j_null(const char *k)           { clave(k); fputs("null", stdout); }
void j_fin(void)                     { fputc('\n', stdout); fflush(stdout); }
