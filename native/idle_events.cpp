#include <KIdleTime>
#include <QGuiApplication>

#include <cstdlib>
#include <iostream>

int main(int argc, char **argv) {
  QGuiApplication application(argc, argv);
  const int timeout_ms = argc > 1 ? std::atoi(argv[1]) : 8000;
  auto *idle = KIdleTime::instance();

  QObject::connect(idle, &KIdleTime::timeoutReached, [idle](int, int) {
    std::cout << "idle" << std::endl;
    idle->catchNextResumeEvent();
  });
  QObject::connect(idle, &KIdleTime::resumingFromIdle,
                   []() { std::cout << "active" << std::endl; });

  idle->addIdleTimeout(timeout_ms);
  if (idle->idleTime() >= timeout_ms) {
    std::cout << "idle" << std::endl;
    idle->catchNextResumeEvent();
  } else {
    std::cout << "active" << std::endl;
  }
  return application.exec();
}
