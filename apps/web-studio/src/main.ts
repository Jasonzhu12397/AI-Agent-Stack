import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./styles.css";
import { installI18n } from "./i18n";

const app = createApp(App).use(createPinia()).use(ElementPlus);
app.mount("#app");
installI18n(document.body);
