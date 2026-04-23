{pkgs}: {
  deps = [
    pkgs.libgbm
    pkgs.xorg.libxkbfile
    pkgs.xorg.libxcb
    pkgs.xorg.libXrandr
    pkgs.xorg.libXfixes
    pkgs.xorg.libXext
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.xorg.libX11
    pkgs.dbus
    pkgs.at-spi2-core
    pkgs.libxkbcommon
    pkgs.mesa
    pkgs.alsa-lib
    pkgs.cairo
    pkgs.pango
    pkgs.gtk3
    pkgs.libdrm
    pkgs.cups
    pkgs.atk
    pkgs.nspr
    pkgs.nss
    pkgs.glib
  ];
}
