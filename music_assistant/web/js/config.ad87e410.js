(window["webpackJsonp"]=window["webpackJsonp"]||[]).push([["config"],{"0c18":function(t,e,n){},1071:function(t,e,n){"use strict";n.r(e);var i=function(){var t=this,e=t.$createElement,i=t._self._c||e;return i("section",[i("v-alert",{attrs:{value:t.restart_message,type:"info"}},[t._v(" "+t._s(t.$t("reboot_required"))+" ")]),t.configKey?t._e():i("v-card",{attrs:{flat:""}},[i("v-list",{attrs:{tile:""}},t._l(t.conf,(function(e,n){return i("v-list-item",{key:n,attrs:{tile:""},on:{click:function(e){return t.$router.push("/config/"+n)}}},[i("v-list-item-content",[i("v-list-item-title",[t._v(" "+t._s(t.$t("conf."+n)))])],1)],1)})),1)],1),"player_settings"!=t.configKey?i("v-card",{attrs:{flat:""}},[i("v-list",{attrs:{"two-line":"",tile:""}},t._l(t.conf[t.configKey],(function(e,s){return i("v-list-group",{key:s,attrs:{"no-action":""},scopedSlots:t._u([{key:"activator",fn:function(){return[i("v-list-item",[i("v-list-item-avatar",{staticStyle:{"margin-left":"-15px"},attrs:{tile:""}},[i("img",{staticStyle:{"border-radius":"5px",border:"1px solid rgba(0,0,0,.85)"},attrs:{src:n("9e01")("./"+s+".png")}})]),i("v-list-item-content",[i("v-list-item-title",[t._v(t._s(t.$t("conf."+s)))])],1)],1)]},proxy:!0}],null,!0)},[t._l(t.conf[t.configKey][s].__desc__,(function(e,n){return i("div",{key:n},[i("v-list-item",["boolean"==typeof e[1]?i("v-switch",{attrs:{label:t.$t("conf."+e[2])},on:{change:function(e){return t.confChanged(t.configKey,s,t.conf[t.configKey][s])}},model:{value:t.conf[t.configKey][s][e[0]],callback:function(n){t.$set(t.conf[t.configKey][s],e[0],n)},expression:"conf[configKey][conf_subkey][conf_item_value[0]]"}}):"<password>"==e[1]?i("v-text-field",{attrs:{label:t.$t("conf."+e[2]),filled:"",type:"password"},on:{change:function(e){return t.confChanged(t.configKey,s,t.conf[t.configKey][s])}},model:{value:t.conf[t.configKey][s][e[0]],callback:function(n){t.$set(t.conf[t.configKey][s],e[0],n)},expression:"conf[configKey][conf_subkey][conf_item_value[0]]"}}):"<player>"==e[1]?i("v-select",{attrs:{label:t.$t("conf."+e[2]),filled:"",type:"password"},on:{change:function(e){return t.confChanged(t.configKey,s,t.conf[t.configKey][s])}},model:{value:t.conf[t.configKey][s][e[0]],callback:function(n){t.$set(t.conf[t.configKey][s],e[0],n)},expression:"conf[configKey][conf_subkey][conf_item_value[0]]"}}):i("v-text-field",{attrs:{label:t.$t("conf."+e[2]),filled:""},on:{change:function(e){return t.confChanged(t.configKey,s,t.conf[t.configKey][s])}},model:{value:t.conf[t.configKey][s][e[0]],callback:function(n){t.$set(t.conf[t.configKey][s],e[0],n)},expression:"conf[configKey][conf_subkey][conf_item_value[0]]"}})],1)],1)})),i("v-divider")],2)})),1)],1):t._e(),"player_settings"==t.configKey?i("v-card",{attrs:{flat:""}},[i("v-list",{attrs:{"two-line":""}},t._l(t.$server.players,(function(e,s){return i("v-list-group",{key:s,attrs:{"no-action":""},scopedSlots:t._u([{key:"activator",fn:function(){return[i("v-list-item",[i("v-list-item-avatar",{staticStyle:{"margin-left":"-20px","margin-right":"6px"},attrs:{tile:""}},[i("img",{staticStyle:{"border-radius":"5px",border:"1px solid rgba(0,0,0,.85)"},attrs:{src:n("9e01")("./"+e.player_provider+".png")}})]),i("v-list-item-content",[i("v-list-item-title",{staticClass:"title"},[t._v(t._s(e.name))]),i("v-list-item-subtitle",{staticClass:"caption"},[t._v(t._s(s))])],1)],1)]},proxy:!0}],null,!0)},[t.conf.player_settings[s].enabled?i("div",t._l(t.conf.player_settings[s].__desc__,(function(e,n){return i("div",{key:n},[i("v-list-item",["boolean"==typeof e[1]?i("v-switch",{attrs:{label:t.$t("conf."+e[2])},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}}):"<password>"==e[1]?i("v-text-field",{attrs:{label:t.$t("conf."+e[2]),filled:"",type:"password"},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}}):"<player>"==e[1]?i("v-select",{attrs:{label:t.$t("conf."+e[2]),filled:""},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}},t._l(t.$server.players,(function(e,n){return i("option",{key:n,domProps:{value:t.item.id}},[t._v(t._s(t.item.name))])})),0):"max_sample_rate"==e[0]?i("v-select",{attrs:{label:t.$t("conf."+e[2]),items:t.sample_rates,filled:""},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}}):"crossfade_duration"==e[0]?i("v-slider",{attrs:{label:t.$t("conf."+e[2]),min:"0",max:"10",filled:"","thumb-label":""},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}}):i("v-text-field",{attrs:{label:t.$t("conf."+e[2]),filled:""},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s][e[0]],callback:function(n){t.$set(t.conf.player_settings[s],e[0],n)},expression:"conf.player_settings[key][conf_item_value[0]]"}})],1),t.conf.player_settings[s].enabled?t._e():i("v-list-item",[i("v-switch",{attrs:{label:t.$t("conf.enabled")},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s].enabled,callback:function(e){t.$set(t.conf.player_settings[s],"enabled",e)},expression:"conf.player_settings[key].enabled"}})],1)],1)})),0):i("div",[i("v-list-item",[i("v-switch",{attrs:{label:t.$t("conf.enabled")},on:{change:function(e){return t.confChanged("player_settings",s,t.conf.player_settings[s])}},model:{value:t.conf.player_settings[s].enabled,callback:function(e){t.$set(t.conf.player_settings[s],"enabled",e)},expression:"conf.player_settings[key].enabled"}})],1)],1),i("v-divider")],1)})),1)],1):t._e()],1)},s=[],o=(n("96cf"),n("c964")),a={components:{},props:["configKey"],data:function(){return{conf:{},players:{},active:0,sample_rates:[44100,48e3,88200,96e3,192e3,384e3],restart_message:!1}},created:function(){this.$store.windowtitle=this.$t("settings"),this.configKey&&(this.$store.windowtitle+=" | "+this.$t("conf."+this.configKey)),this.getConfig()},methods:{getConfig:function(){var t=this;return Object(o["a"])(regeneratorRuntime.mark((function e(){return regeneratorRuntime.wrap((function(e){while(1)switch(e.prev=e.next){case 0:return e.next=2,t.$server.getData("config");case 2:t.conf=e.sent;case 3:case"end":return e.stop()}}),e)})))()},confChanged:function(t,e,n){var i=this;return Object(o["a"])(regeneratorRuntime.mark((function s(){var o,a;return regeneratorRuntime.wrap((function(s){while(1)switch(s.prev=s.next){case 0:return o="config/"+t+"/"+e,s.next=3,i.$server.putData(o,n);case 3:a=s.sent,a.restart_required&&(i.restart_message=!0);case 5:case"end":return s.stop()}}),s)})))()}}},r=a,l=n("2877"),c=n("6544"),u=n.n(c),f=(n("caad"),n("f3f3")),h=n("fc11"),d=(n("0c18"),n("10d2")),p=n("afdd"),g=n("9d26"),v=n("f2e7"),y=n("7560"),m=n("2b0e"),_=m["a"].extend({name:"transitionable",props:{mode:String,origin:String,transition:String}}),b=n("58df"),C=n("d9bd"),$=Object(b["a"])(d["a"],v["a"],_).extend({name:"v-alert",props:{border:{type:String,validator:function(t){return["top","right","bottom","left"].includes(t)}},closeLabel:{type:String,default:"$vuetify.close"},coloredBorder:Boolean,dense:Boolean,dismissible:Boolean,closeIcon:{type:String,default:"$cancel"},icon:{default:"",type:[Boolean,String],validator:function(t){return"string"===typeof t||!1===t}},outlined:Boolean,prominent:Boolean,text:Boolean,type:{type:String,validator:function(t){return["info","error","success","warning"].includes(t)}},value:{type:Boolean,default:!0}},computed:{__cachedBorder:function(){if(!this.border)return null;var t={staticClass:"v-alert__border",class:Object(h["a"])({},"v-alert__border--".concat(this.border),!0)};return this.coloredBorder&&(t=this.setBackgroundColor(this.computedColor,t),t.class["v-alert__border--has-color"]=!0),this.$createElement("div",t)},__cachedDismissible:function(){var t=this;if(!this.dismissible)return null;var e=this.iconColor;return this.$createElement(p["a"],{staticClass:"v-alert__dismissible",props:{color:e,icon:!0,small:!0},attrs:{"aria-label":this.$vuetify.lang.t(this.closeLabel)},on:{click:function(){return t.isActive=!1}}},[this.$createElement(g["a"],{props:{color:e}},this.closeIcon)])},__cachedIcon:function(){return this.computedIcon?this.$createElement(g["a"],{staticClass:"v-alert__icon",props:{color:this.iconColor}},this.computedIcon):null},classes:function(){var t=Object(f["a"])(Object(f["a"])({},d["a"].options.computed.classes.call(this)),{},{"v-alert--border":Boolean(this.border),"v-alert--dense":this.dense,"v-alert--outlined":this.outlined,"v-alert--prominent":this.prominent,"v-alert--text":this.text});return this.border&&(t["v-alert--border-".concat(this.border)]=!0),t},computedColor:function(){return this.color||this.type},computedIcon:function(){return!1!==this.icon&&("string"===typeof this.icon&&this.icon?this.icon:!!["error","info","success","warning"].includes(this.type)&&"$".concat(this.type))},hasColoredIcon:function(){return this.hasText||Boolean(this.border)&&this.coloredBorder},hasText:function(){return this.text||this.outlined},iconColor:function(){return this.hasColoredIcon?this.computedColor:void 0},isDark:function(){return!(!this.type||this.coloredBorder||this.outlined)||y["a"].options.computed.isDark.call(this)}},created:function(){this.$attrs.hasOwnProperty("outline")&&Object(C["a"])("outline","outlined",this)},methods:{genWrapper:function(){var t=[this.$slots.prepend||this.__cachedIcon,this.genContent(),this.__cachedBorder,this.$slots.append,this.$scopedSlots.close?this.$scopedSlots.close({toggle:this.toggle}):this.__cachedDismissible],e={staticClass:"v-alert__wrapper"};return this.$createElement("div",e,t)},genContent:function(){return this.$createElement("div",{staticClass:"v-alert__content"},this.$slots.default)},genAlert:function(){var t={staticClass:"v-alert",attrs:{role:"alert"},on:this.listeners$,class:this.classes,style:this.styles,directives:[{name:"show",value:this.isActive}]};if(!this.coloredBorder){var e=this.hasText?this.setTextColor:this.setBackgroundColor;t=e(this.computedColor,t)}return this.$createElement("div",t,[this.genWrapper()])},toggle:function(){this.isActive=!this.isActive}},render:function(t){var e=this.genAlert();return this.transition?t("transition",{props:{name:this.transition,origin:this.origin,mode:this.mode}},[e]):e}}),w=n("b0af"),k=n("ce7e"),x=n("8860"),S=n("56b0"),V=n("da13"),K=n("8270"),B=n("5d23"),A=n("b974"),I=n("ba0d"),D=(n("0481"),n("4069"),n("ec29"),n("9d01"),n("4de4"),n("45fc"),n("d3b7"),n("25f0"),n("c37a")),O=n("5607"),j=m["a"].extend({name:"rippleable",directives:{ripple:O["a"]},props:{ripple:{type:[Boolean,Object],default:!0}},methods:{genRipple:function(){var t=arguments.length>0&&void 0!==arguments[0]?arguments[0]:{};return this.ripple?(t.staticClass="v-input--selection-controls__ripple",t.directives=t.directives||[],t.directives.push({name:"ripple",value:{center:!0}}),this.$createElement("div",t)):null}}}),E=n("8547");function L(t){t.preventDefault()}var T=Object(b["a"])(D["a"],j,E["a"]).extend({name:"selectable",model:{prop:"inputValue",event:"change"},props:{id:String,inputValue:null,falseValue:null,trueValue:null,multiple:{type:Boolean,default:null},label:String},data:function(){return{hasColor:this.inputValue,lazyValue:this.inputValue}},computed:{computedColor:function(){if(this.isActive)return this.color?this.color:this.isDark&&!this.appIsDark?"white":"primary"},isMultiple:function(){return!0===this.multiple||null===this.multiple&&Array.isArray(this.internalValue)},isActive:function(){var t=this,e=this.value,n=this.internalValue;return this.isMultiple?!!Array.isArray(n)&&n.some((function(n){return t.valueComparator(n,e)})):void 0===this.trueValue||void 0===this.falseValue?e?this.valueComparator(e,n):Boolean(n):this.valueComparator(n,this.trueValue)},isDirty:function(){return this.isActive},rippleState:function(){return this.isDisabled||this.validationState?this.validationState:void 0}},watch:{inputValue:function(t){this.lazyValue=t,this.hasColor=t}},methods:{genLabel:function(){var t=D["a"].options.methods.genLabel.call(this);return t?(t.data.on={click:L},t):t},genInput:function(t,e){return this.$createElement("input",{attrs:Object.assign({"aria-checked":this.isActive.toString(),disabled:this.isDisabled,id:this.computedId,role:t,type:t},e),domProps:{value:this.value,checked:this.isActive},on:{blur:this.onBlur,change:this.onChange,focus:this.onFocus,keydown:this.onKeydown,click:L},ref:"input"})},onBlur:function(){this.isFocused=!1},onClick:function(t){this.onChange(),this.$emit("click",t)},onChange:function(){var t=this;if(this.isInteractive){var e=this.value,n=this.internalValue;if(this.isMultiple){Array.isArray(n)||(n=[]);var i=n.length;n=n.filter((function(n){return!t.valueComparator(n,e)})),n.length===i&&n.push(e)}else n=void 0!==this.trueValue&&void 0!==this.falseValue?this.valueComparator(n,this.trueValue)?this.falseValue:this.trueValue:e?this.valueComparator(n,e)?null:e:!n;this.validate(!0,n),this.internalValue=n,this.hasColor=n}},onFocus:function(){this.isFocused=!0},onKeydown:function(t){}}}),R=n("c3f0"),F=n("0789"),P=n("490a"),z=n("80d2"),M=T.extend({name:"v-switch",directives:{Touch:R["a"]},props:{inset:Boolean,loading:{type:[Boolean,String],default:!1},flat:{type:Boolean,default:!1}},computed:{classes:function(){return Object(f["a"])(Object(f["a"])({},D["a"].options.computed.classes.call(this)),{},{"v-input--selection-controls v-input--switch":!0,"v-input--switch--flat":this.flat,"v-input--switch--inset":this.inset})},attrs:function(){return{"aria-checked":String(this.isActive),"aria-disabled":String(this.isDisabled),role:"switch"}},validationState:function(){return this.hasError&&this.shouldValidate?"error":this.hasSuccess?"success":null!==this.hasColor?this.computedColor:void 0},switchData:function(){return this.setTextColor(this.loading?void 0:this.validationState,{class:this.themeClasses})}},methods:{genDefaultSlot:function(){return[this.genSwitch(),this.genLabel()]},genSwitch:function(){return this.$createElement("div",{staticClass:"v-input--selection-controls__input"},[this.genInput("checkbox",Object(f["a"])(Object(f["a"])({},this.attrs),this.attrs$)),this.genRipple(this.setTextColor(this.validationState,{directives:[{name:"touch",value:{left:this.onSwipeLeft,right:this.onSwipeRight}}]})),this.$createElement("div",Object(f["a"])({staticClass:"v-input--switch__track"},this.switchData)),this.$createElement("div",Object(f["a"])({staticClass:"v-input--switch__thumb"},this.switchData),[this.genProgress()])])},genProgress:function(){return this.$createElement(F["c"],{},[!1===this.loading?null:this.$slots.progress||this.$createElement(P["a"],{props:{color:!0===this.loading||""===this.loading?this.color||"primary":this.loading,size:16,width:2,indeterminate:!0}})])},onSwipeLeft:function(){this.isActive&&this.onChange()},onSwipeRight:function(){this.isActive||this.onChange()},onKeydown:function(t){(t.keyCode===z["w"].left&&this.isActive||t.keyCode===z["w"].right&&!this.isActive)&&this.onChange()}}}),q=n("8654"),J=Object(l["a"])(r,i,s,!1,null,null,null);e["default"]=J.exports;u()(J,{VAlert:$,VCard:w["a"],VDivider:k["a"],VList:x["a"],VListGroup:S["a"],VListItem:V["a"],VListItemAvatar:K["a"],VListItemContent:B["a"],VListItemSubtitle:B["b"],VListItemTitle:B["c"],VSelect:A["a"],VSlider:I["a"],VSwitch:M,VTextField:q["a"]})},"9d01":function(t,e,n){},ec29:function(t,e,n){}}]);
//# sourceMappingURL=config.ad87e410.js.map