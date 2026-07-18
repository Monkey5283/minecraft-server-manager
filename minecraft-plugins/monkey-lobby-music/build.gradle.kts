plugins {
    java
}

group = "dev.monkeycraft"
version = "1.0.0"

repositories {
    maven {
        name = "papermc"
        url = uri("https://repo.papermc.io/repository/maven-public/")
    }
    mavenCentral()
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:1.20.1-R0.1-SNAPSHOT")
    testImplementation(platform("org.junit:junit-bom:5.14.2"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(17))
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

tasks.processResources {
    filesMatching("plugin.yml") {
        expand("version" to project.version)
    }
    from("../../minecraft-audio/monkeycraft-lobby") {
        include("monkeycraft_nexus_awaits.nbs")
        include("monkeycraft_festival_of_the_skyways.nbs")
        into("songs")
    }
}

tasks.test {
    useJUnitPlatform()
}

tasks.jar {
    archiveFileName.set("MonkeyLobbyMusic-${project.version}.jar")
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
}

tasks.register<Copy>("stageDistribution") {
    dependsOn(tasks.jar)
    from(tasks.jar.flatMap { it.archiveFile })
    into(layout.projectDirectory.dir("dist"))
    rename { "MonkeyLobbyMusic.jar" }
}
