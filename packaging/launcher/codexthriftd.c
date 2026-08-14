#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if defined(__arm64__)
#define CODEXTHRIFT_ARCH "arm64"
#elif defined(__x86_64__)
#define CODEXTHRIFT_ARCH "x86_64"
#else
#error Unsupported macOS architecture
#endif

static int executable_directory(char *output, size_t output_size) {
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        return -1;
    }
    char resolved[PATH_MAX];
    const char *path = realpath(executable, resolved) != NULL ? resolved : executable;
    if (strlen(path) + 1 > output_size) {
        return -1;
    }
    strcpy(output, path);
    char *separator = strrchr(output, '/');
    if (separator == NULL) {
        return -1;
    }
    *separator = '\0';
    return 0;
}

static int executable_path(char *output, size_t output_size) {
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        return -1;
    }
    char resolved[PATH_MAX];
    const char *path = realpath(executable, resolved) != NULL ? resolved : executable;
    if (strlen(path) + 1 > output_size) {
        return -1;
    }
    strcpy(output, path);
    return 0;
}

static int candidate_python(const char *root, char *output, size_t output_size) {
    if (snprintf(output, output_size, "%s/bin/python", root) >= (int)output_size) {
        return -1;
    }
    return access(output, X_OK) == 0 ? 0 : -1;
}

static int resolve_runtime(char *root, size_t root_size, char *python, size_t python_size) {
    const char *configured = getenv("CODEXTHRIFT_RUNTIME_ROOT");
    if (configured != NULL && configured[0] != '\0') {
        if (strlen(configured) + 1 <= root_size) {
            strcpy(root, configured);
            if (candidate_python(root, python, python_size) == 0) {
                return 0;
            }
        }
        return -1;
    }

    char directory[PATH_MAX];
    if (executable_directory(directory, sizeof(directory)) != 0) {
        return -1;
    }
    const char *templates[] = {
        "%s/../Resources/codexthrift-runtime",
        "%s/codexthrift-runtime",
        "%s/../codexthrift-runtime",
    };
    for (size_t index = 0; index < sizeof(templates) / sizeof(templates[0]); index++) {
        if (snprintf(root, root_size, templates[index], directory) >= (int)root_size) {
            continue;
        }
        if (candidate_python(root, python, python_size) == 0) {
            return 0;
        }
    }
    return -1;
}

int main(int argc, char **argv) {
    char runtime_root[PATH_MAX];
    char python[PATH_MAX];
    if (resolve_runtime(runtime_root, sizeof(runtime_root), python, sizeof(python)) != 0) {
        fprintf(stderr, "CodexThrift Runtime was not found. Set CODEXTHRIFT_RUNTIME_ROOT or reinstall the App.\n");
        return 78;
    }
    setenv("CODEXTHRIFT_RUNTIME_ROOT", runtime_root, 1);
    setenv("CODEXTHRIFT_RUNTIME_ARCH", CODEXTHRIFT_ARCH, 1);
    char launcher_path[PATH_MAX];
    if (executable_path(launcher_path, sizeof(launcher_path)) == 0) {
        setenv("CODEXTHRIFT_LAUNCHER_PATH", launcher_path, 1);
    }

    char **child_argv = calloc((size_t)argc + 4, sizeof(char *));
    if (child_argv == NULL) {
        fprintf(stderr, "Unable to allocate daemon arguments.\n");
        return 70;
    }
    child_argv[0] = python;
    child_argv[1] = "-m";
    child_argv[2] = "codexthrift.daemon.main";
    for (int index = 1; index < argc; index++) {
        child_argv[index + 2] = argv[index];
    }
    child_argv[argc + 2] = NULL;
    execv(python, child_argv);
    fprintf(stderr, "Unable to execute CodexThrift Runtime: %s\n", strerror(errno));
    free(child_argv);
    return 69;
}
