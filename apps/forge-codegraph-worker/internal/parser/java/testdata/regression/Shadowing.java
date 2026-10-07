class Shadowing {
  Service service;
  void run() {
    service.save();
    { Other service = null; service.save(); }
    service.save();
  }
}
