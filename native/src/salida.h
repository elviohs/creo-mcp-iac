/* creo_iac_tk - salida JSON minima (UTF-8) */
#ifndef CREO_IAC_SALIDA_H
#define CREO_IAC_SALIDA_H
#include <wchar.h>

void j_abrir_obj(void);
void j_cerrar_obj(void);
void j_abrir_lista(const char *clave);
void j_cerrar_lista(void);
void j_clave_obj(const char *clave);
void j_str(const char *clave, const char *valor);
void j_wstr(const char *clave, const wchar_t *valor);
void j_int(const char *clave, long valor);
void j_num(const char *clave, double valor);
void j_bool(const char *clave, int valor);
void j_null(const char *clave);
/* Termina el documento y lo escribe en stdout */
void j_fin(void);

/* Conversion wchar_t -> UTF-8 (win_utf8.c) */
int a_utf8(const wchar_t *w, char *buf, int tam);
#endif
