/* creo_iac_tk - conversion a UTF-8 (separado para no mezclar windows.h con Toolkit) */
#include <windows.h>
#include <wchar.h>

int a_utf8(const wchar_t *w, char *buf, int tam)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, buf, tam, NULL, NULL);
    if (n <= 0) { if (tam > 0) buf[0] = '\0'; return 0; }
    return n;
}
