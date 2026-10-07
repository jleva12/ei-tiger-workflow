"""Generates the torture repository: real-world edge cases that must never
fail an ingestion run, in Java (a Maven reactor), TypeScript/JavaScript and
Python. Every case is something found in real code bases: legacy encodings,
generated code, huge literals, deep nesting, broken files, odd paths.

    python generate.py <empty-directory>

The directory becomes a Git repository with one commit on main. Run it
through TestIngestLocalRepositories: the run must succeed.
"""

import os
import random
import subprocess
import sys

random.seed(7)


def write(root: str, path: str, content, mode: str = "w", encoding: str = "utf-8") -> None:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if isinstance(content, bytes):
        with open(full, "wb") as f:
            f.write(content)
    else:
        with open(full, mode, encoding=encoding, newline="") as f:
            f.write(content)


def pom(artifact: str, deps=(), extra: str = "") -> str:
    dependencies = "".join(
        f"<dependency><groupId>torture</groupId><artifactId>{d}</artifactId><version>1</version></dependency>" for d in deps
    )
    return (
        "<project><modelVersion>4.0.0</modelVersion>"
        "<parent><groupId>torture</groupId><artifactId>parent</artifactId><version>1</version></parent>"
        f"<artifactId>{artifact}</artifactId><dependencies>{dependencies}</dependencies>{extra}</project>"
    )


def java(root: str) -> None:
    write(
        root,
        "pom.xml",
        "<project><modelVersion>4.0.0</modelVersion><groupId>torture</groupId><artifactId>parent</artifactId>"
        "<version>1</version><packaging>pom</packaging>"
        "<properties><maven.compiler.release>21</maven.compiler.release>"
        "<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
        "<modules><module>core</module><module>app</module><module>broken</module><module>jpms</module></modules>"
        "<build><pluginManagement><plugins>"
        "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-install-plugin</artifactId><version>3.1.3</version></plugin>"
        "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>"
        "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-jar-plugin</artifactId><version>3.4.2</version></plugin>"
        "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin>"
        "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin>"
        "</plugins></pluginManagement></build></project>",
    )
    write(root, "core/pom.xml", pom("core"))
    write(root, "app/pom.xml", pom("app", deps=["core"]))
    write(root, "broken/pom.xml", pom("broken", deps=["core"]))
    write(root, "jpms/pom.xml", pom("jpms"))
    base = "core/src/main/java/t/core/"

    # The API everything else calls.
    write(root, base + "Api.java", "package t.core;\npublic class Api {\n  public String call(String s) { return s; }\n  public static Api make() { return new Api(); }\n}\n")
    # Identifiers and text beyond ASCII, and a unicode escape in an identifier.
    write(root, base + "Unicode.java", "package t.core;\npublic class Unicode {\n  public int π = 3;\n  int \\u0061bc = 1; // abc\n  String emoji = \"🚀 ✓ ﷽ \\u202e reversed\";\n  public class Größe { }\n}\n")
    # A 200 KB constant and a 100 KB text block.
    blob = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(200_000))
    block = "\n".join("      line " + str(i) + " " + "x" * 80 for i in range(1_200))
    write(root, base + "HugeString.java", f'package t.core;\npublic class HugeString {{\n  public static final String BLOB = "{blob}";\n  public static final String BLOCK = """\n{block}\n      """;\n}}\n')
    # A 100 KB Javadoc.
    doc = "\n".join(" * " + "documentation " * 10 for _ in range(700))
    write(root, base + "HugeJavadoc.java", f"package t.core;\n/**\n{doc}\n */\npublic class HugeJavadoc {{ public void run() {{ }} }}\n")
    # A table of 5,000 user agents in one field, like the one pitbull had.
    agents = ",\n".join(f'    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{i}.0 Safari/537.36"' for i in range(5_000))
    write(root, base + "UserAgents.java", f"package t.core;\npublic class UserAgents {{\n  private final String[] agents = {{\n{agents}\n  }};\n  public String first() {{ return agents[0]; }}\n}}\n")
    # Nesting deeper than the parser's depth limit, and generated code that
    # nests deeper still: a long concatenation and a long builder chain.
    write(root, base + "DeepNest.java", "package t.core;\npublic class DeepNest {\n  int x = " + "(" * 400 + "1" + ")" * 400 + ";\n}\n")
    concat = " + ".join(f'"part{i}"' for i in range(3_000))
    write(root, base + "LongConcat.java", f"package t.core;\npublic class LongConcat {{\n  public static final String SQL = {concat};\n}}\n")
    chain = "".join(".self()" for _ in range(2_000))
    write(root, base + "LongChain.java", f"package t.core;\npublic class LongChain {{\n  LongChain self() {{ return this; }}\n  void run() {{ new LongChain(){chain}; }}\n}}\n")
    # Huge but flat declarations.
    constants = ",\n".join(f"  C{i}" for i in range(3_000))
    write(root, base + "BigEnum.java", f"package t.core;\npublic enum BigEnum {{\n{constants};\n  public String low() {{ return name().toLowerCase(); }}\n}}\n")
    params = ", ".join(f"int p{i}" for i in range(255))
    write(root, base + "ManyParams.java", f"package t.core;\npublic class ManyParams {{\n  public int sum({params}) {{ return p0 + p254; }}\n}}\n")
    fields = "\n".join(f"  public int f{i};" for i in range(10_000))
    write(root, base + "ManyFields.java", f"package t.core;\npublic class ManyFields {{\n{fields}\n}}\n")
    # Modern Java: records, sealed types, pattern switches, local records.
    write(root, base + "Modern.java", "package t.core;\npublic class Modern {\n  public sealed interface Shape permits Circle, Square {}\n  public record Circle(double r) implements Shape {}\n  public record Square(double s) implements Shape {}\n  public static double area(Shape s) {\n    return switch (s) {\n      case Circle c when c.r() > 0 -> Math.PI * c.r() * c.r();\n      case Circle c -> 0;\n      case Square q -> q.s() * q.s();\n    };\n  }\n  void local() { record Point(int x, int y) {} var p = new Point(1, 2); Runnable r = () -> System.out.println(p.x()); }\n}\n")
    write(root, base + "package-info.java", "/** The core package. */\n@Deprecated\npackage t.core;\n")
    write(root, "core/src/main/java/DefaultPackage.java", "public class DefaultPackage { public t.core.Api api() { return t.core.Api.make(); } }\n")
    # Legacy encodings and line endings: Latin-1 in a comment, a string and an
    # identifier; a UTF-8 BOM; CRLF and bare CR; a raw NUL; UTF-16.
    write(root, base + "Latin1.java", "package t.core;\n// Réalisé par l'équipe\npublic class Latin1 {\n  String café = \"crème brûlée\";\n  int résumé = 1;\n}\n", encoding="latin-1")
    write(root, base + "Bom.java", "\ufeffpackage t.core;\npublic class Bom { }\n")
    write(root, base + "Crlf.java", "package t.core;\r\npublic class Crlf {\r\n  int a = 1;\r\n}\r\n")
    write(root, base + "LoneCr.java", "package t.core;\rpublic class LoneCr {\r  int a = 1;\r}\r")
    write(root, base + "Nul.java", b'package t.core;\npublic class Nul {\n  String s = "a\x00b";\n}\n')
    write(root, base + "Utf16.java", "package t.core;\npublic class Utf16 { int a = 1; }\n".encode("utf-16"))
    # Broken, empty, duplicate and misplaced files.
    write(root, base + "Broken.java", "package t.core;\npublic class Broken {\n  void run( {\n    int = ;\n")
    write(root, base + "Empty.java", "")
    write(root, base + "Dup1.java", "package t.core;\nclass Same { int a; }\n")
    write(root, base + "Dup2.java", "package t.core;\nclass Same { int b; }\n")
    write(root, base + "WrongDir.java", "package t.elsewhere;\npublic class WrongDir { }\n")
    write(root, base + "With Space.java", "package t.core;\nclass WithSpace { }\n")
    write(root, base + "Ünïcødé.java", "package t.core;\nclass Unicodefile { }\n")
    write(root, base + ("L" * 180) + ".java", "package t.core;\nclass LongName { }\n")
    write(root, base + "NotReallyJava.java", bytes(random.getrandbits(8) for _ in range(20_000)))
    # Symlinks: to a file inside the repository, outside it, and a loop.
    write(root, "shared/Linked.java", "package t.core;\npublic class Linked { }\n")
    os.symlink("../../../../../../shared/Linked.java", os.path.join(root, base + "Linked.java"))
    os.symlink("/etc/hosts", os.path.join(root, base + "Outside.java"))
    os.makedirs(os.path.join(root, base + "loop"), exist_ok=True)
    os.symlink("..", os.path.join(root, base + "loop/again"))

    # Tests that call core.
    write(root, "core/src/test/java/t/core/ApiTest.java", "package t.core;\nclass ApiTest { void t() { new Api().call(\"x\"); Api.make(); } }\n")
    # app calls core; broken does not compile; jpms is a named module.
    write(root, "app/src/main/java/t/app/Main.java", "package t.app;\nimport t.core.Api;\npublic class Main { public static void main(String[] a) { System.out.println(new Api().call(\"hi\")); } }\n")
    write(root, "broken/src/main/java/t/broken/Bad.java", "package t.broken;\npublic class Bad { String s = t.core.Api.make().call(missing()); }\n")
    write(root, "broken/src/main/java/t/broken/Good.java", "package t.broken;\npublic class Good { public String ok() { return t.core.Api.make().call(\"ok\"); } }\n")
    write(root, "jpms/src/main/java/module-info.java", "module torture.jpms { exports t.jpms; requires java.logging; }\n")
    write(root, "jpms/src/main/java/t/jpms/Named.java", "package t.jpms;\npublic class Named { java.util.logging.Logger log = java.util.logging.Logger.getLogger(\"x\"); }\n")


def web(root: str) -> None:
    write(root, "web/package.json", '{"name": "torture-web", "private": true}\n')
    write(root, "web/tsconfig.json", '{\n  // comment\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}, "jsx": "react-jsx", "outDir": "dist",},\n}\n')
    write(root, "web/src/api.ts", "export class Api { call(s: string): string { return s; } }\nexport default function make(): Api { return new Api(); }\n")
    write(root, "web/src/app.tsx", 'import make, { Api } from "@/api";\nimport type { Shape } from "./types";\n@sealed\nclass View { api: Api = make(); render() { return <div onClick={() => this.api.call("x")}>{"hi"}</div>; } }\nfunction sealed(c: Function) {}\nexport const lazy = () => import("./api");\nexport enum Color { Red, Green }\nexport namespace NS { export const x = 1; }\nexport type { Shape };\n')
    write(root, "web/src/types.d.ts", "export interface Shape { area(): number }\ndeclare module 'untyped' { const x: any; export = x; }\n")
    write(root, "web/src/legacy.js", "module.exports = { run: function () { return require('./api'); } };\n")
    write(root, "web/src/esm.mjs", "export const x = 1;\nexport default x;\n")
    write(root, "web/src/cjs.cjs", "exports.y = 2;\n")
    bundle = "var a=0;" + ";".join(f"function f{i}(x){{return x+{i}}}" for i in range(40_000))
    write(root, "web/src/vendor.min.js", bundle + "\n")
    write(root, "web/src/mapped.js", "export const z = 3;\n//# sourceMappingURL=mapped.js.map\n")
    write(root, "web/src/broken.ts", "export function broken( {\n  const = ;\n")
    write(root, "web/src/deep.ts", "export const deep = " + "{a:" * 400 + "1" + "}" * 400 + ";\n")
    items = ",".join(f'"item-{i}-{random.getrandbits(64):x}"' for i in range(20_000))
    write(root, "web/src/bigarray.ts", f"export const items = [{items}];\n")
    write(root, "web/src/template.ts", "export const t = `" + "row ${1}\\n".replace("${1}", "") * 30_000 + "`;\n")
    write(root, "web/src/bom.ts", "\ufeffexport const bom = 1;\n")
    write(root, "web/src/latin1.ts", "// réalisé\nexport const café = 'crème';\n", encoding="latin-1")
    write(root, "web/src/utf16.ts", "export const u = 1;\n".encode("utf-16"))
    write(root, "web/src/empty.ts", "")
    for i in range(3_000):
        write(root, f"web/src/gen/g{i}.ts", f'import {{ Api }} from "../api";\nexport function g{i}(a: Api): string {{ return a.call("{i}"); }}\n')


def python(root: str) -> None:
    write(root, "py/pyproject.toml", '[project]\nname = "torture"\nversion = "1"\n')
    write(root, "py/torture/__init__.py", "")
    write(root, "py/torture/api.py", "class Api:\n    def call(self, s: str) -> str:\n        return s\n\n\ndef make() -> Api:\n    return Api()\n")
    write(root, "py/torture/use.py", "from .api import Api, make\nfrom . import api as a\nfrom torture.api import *\n\n\ndef run() -> str:\n    return make().call('x') + a.make().call('y')\n")
    write(root, "py/torture/modern.py", "import dataclasses\n\n@dataclasses.dataclass\nclass P:\n    x: int\n\n\ndef m(v):\n    match v:\n        case P(x=0):\n            return 0\n        case [a, *rest] if (n := len(rest)) > 1:\n            return n\n        case _:\n            return f\"{v!r:>{10}} {f'{v}'}\"\n")
    write(root, "py/torture/py2.py", "print 'hello'\nexec 'x = 1'\n")
    write(root, "py/torture/broken.py", "def broken(:\n    return\n")
    write(root, "py/torture/deep.py", "x = " + "(" * 400 + "1" + ")" * 400 + "\n")
    write(root, "py/torture/huge.py", "BLOB = '" + "".join(random.choice("abcdef0123456789") for _ in range(200_000)) + "'\n")
    write(root, "py/torture/latin1.py", "# -*- coding: latin-1 -*-\n# réalisé\ncafé = 'crème'\n", encoding="latin-1")
    write(root, "py/torture/bom.py", "\ufeffx = 1\n")
    write(root, "py/torture/tabs.py", "def f():\n\tif True:\n        return 1\n")
    write(root, "py/torture/relative.py", "from ...outside import thing\n")
    write(root, "py/torture/circular_a.py", "from .circular_b import b\n\ndef a():\n    return b()\n")
    write(root, "py/torture/circular_b.py", "from .circular_a import a\n\ndef b():\n    return a()\n")
    write(root, "py/torture/nul.py", b"x = 'a\x00b'\n")


def misc(root: str) -> None:
    write(root, "data/model.bin", "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 12345\n")
    write(root, "data/big.json", "[" + ",".join(str(i) for i in range(2_000_000)) + "]\n")
    write(root, "lib/vendored.jar", bytes(random.getrandbits(8) for _ in range(50_000)))
    write(root, "README.md", "# Torture\n")


def main() -> None:
    root = sys.argv[1]
    os.makedirs(root, exist_ok=True)
    if os.listdir(root):
        sys.exit(f"{root} is not empty")
    java(root)
    web(root)
    python(root)
    misc(root)
    git = ["git", "-C", root, "-c", "user.name=Torture", "-c", "user.email=torture@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(git[:3] + ["init", "--quiet", "-b", "main"], check=True)
    subprocess.run(git + ["add", "--all"], check=True)
    subprocess.run(git + ["commit", "--quiet", "-m", "torture"], check=True)
    print(root)


if __name__ == "__main__":
    main()
