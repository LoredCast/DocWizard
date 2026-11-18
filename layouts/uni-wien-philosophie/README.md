# Universität Wien · Philosophie

Nach dem *Leitfaden für schriftliche Arbeiten im Bachelorstudium Philosophie* (Version 9/2021):

- Titelblatt mit Titel, Untertitel, Art der Arbeit, Lehrveranstaltung, LV-Leitung, Semester, Institut und
  Universität, darunter „vorgelegt von“ mit Name, Matrikelnummer, Studienkennzahl und E-Mail, unten Ort und Datum.
- Plagiatserklärung mit dem Wortlaut des Leitfadens und einer Zeile für Ort, Datum und Unterschrift.
- Inhaltsverzeichnis auf eigener Seite, Dezimalgliederung (1., 1.1., …), wahlweise traditionell (I., 1., a)).
- Tinos (gleiche Laufweite wie Times New Roman), 12 pt, Zeilenabstand 1,5; Blockzitate und Fußnoten
  10 pt einzeilig; Seitenzahlen.
- Belege als Fußnoten („Vorname Nachname: Titel. Ort Jahr, Seite.“, danach „Nachname: Kurztitel, Seite.“
  und „Ebd.“) oder als Kurzbelege („Mustermann 2001, 11“), Literaturverzeichnis mit hängendem Einzug.

Angaben für das Titelblatt in den Metadaten des Dokuments:

```yaml
---
title: "Titel der Arbeit"
subtitle: "Untertitel der Arbeit"
paper-type: Seminararbeit
course: "LV-Nr. 180123: Titel der Lehrveranstaltung"
instructor: Name der LV-Leitung
semester: Wintersemester 2026/27
department: Institut für Philosophie
institution: Universität Wien
author: Name des Verfassers
student-id: "01234567"      # Matrikelnummer
program: "A 033 541"        # Studienkennzahl
email: name@univie.ac.at
place: Wien
date: 16. November 2026
lang: de-AT
---
```

Die E-Mail-Adresse kann auch beim Autor stehen (`author: [{name: …, email: …}]`). Eine eigene
Plagiatserklärung ersetzt `declaration:` in den Metadaten.

Optionen: Gliederung, Inhaltsverzeichnis, Titelblatt und Belegsystem (Fußnoten oder Kurzbelege).
