package p;

import java.util.List;

public class App implements Greeter {
    private final List<String> names = List.of("a", "b");

    @Override
    public String greet(String name) {
        return "Hello " + name;
    }

    @Override
    public String toString() {
        return "App" + names.size();
    }

    public void run() {
        Greeter direct = name -> greet(name) + "!";
        Greeter viaRef = this::greet;
        direct.greet("x");
        viaRef.greet(names.get(0));
    }
}
