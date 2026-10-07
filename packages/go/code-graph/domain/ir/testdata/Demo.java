// π: manually constructed IR example; no parser has run.
package demo;
import outside.Client;
@Route(path = {"/a", "/b"}, produces = "application/json")
class Demo<T> {
    java.util.List<T> values;
    Client service;
    Demo() { this(1); }
    Demo(int n) {}
    void run() {
        service.save(1);
        { Other service = null; service.save(2); }
        service.save(3);
        service.next().save(4);
        java.util.function.Consumer<String> fn = this::accept;
    }
    void accept(String value) {}
}
